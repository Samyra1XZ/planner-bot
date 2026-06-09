import asyncio
import os
import re
import secrets
import tempfile
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler, CallbackQueryHandler, filters,
)

from database import (
    init_db, add_reminder, get_active_reminders, get_all_active_reminders, mark_done,
    delete_reminder, get_reminder_by_id, update_reminder_time, count_done_on,
    ensure_user, get_user_timezone,
)
from scheduler import scheduler, schedule_reminder, reschedule_all, unschedule_reminder, TZ
from stt import transcribe
import brain  # «мозг» разбора на Gemini

# Загружаем переменные из файла .env (BOT_TOKEN, MY_CHAT_ID)
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
MY_CHAT_ID = int(os.getenv("MY_CHAT_ID"))


def is_owner(update: Update) -> bool:
    """Проверяет, что команду отправил именно я, а не кто-то чужой.

    Пока бот персональный — пускаем только владельца. Когда откроем продукт
    для всех, эту проверку снимем (схема БД уже многопользовательская)."""
    return update.effective_user.id == MY_CHAT_ID


def _tz(user_id: int) -> ZoneInfo:
    """Личная таймзона пользователя как ZoneInfo (дефолт — TZ при ошибке)."""
    try:
        return ZoneInfo(get_user_timezone(user_id))
    except Exception:
        return TZ


# ───────────────────────── Меню-клавиатура (интерфейс без команд) ──────────────────────────

# Подписи кнопок нижнего меню. Нажатие присылает ровно этот текст —
# перехватываем его в handle_text ДО разбора через Gemini.
MENU_TODAY = "📋 Сегодня"
MENU_WEEK = "🗓 Неделя"
MENU_BDAY = "🎂 Дни рождения"
MENU_LIST = "📝 Все задачи"
MENU_ADD = "➕ Новая задача"


def build_menu() -> ReplyKeyboardMarkup:
    """Постоянное меню внизу экрана — навигация тапами, без ввода команд."""
    return ReplyKeyboardMarkup(
        [[MENU_TODAY, MENU_WEEK], [MENU_BDAY, MENU_LIST], [MENU_ADD]],
        resize_keyboard=True,    # компактные кнопки по размеру текста
        is_persistent=True,      # меню не прячется после нажатия
    )


# ───────────────────────── Эмодзи по смыслу задачи ──────────────────────────

def pick_emoji(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ("трен", "спорт", "зал", "бег", "йога", "фитнес", "качал", "штанг")):
        return "🏋"
    if any(w in t for w in ("еда", "завтрак", "обед", "ужин", "кафе", "ресторан", "покушать", "поесть")):
        return "🍽"
    if any(w in t for w in ("встреча", "встретиться", "встретить", "свидан")):
        return "🤝"
    if any(w in t for w in ("работа", "задача", "чат", "бот", "код", "программ", "разработка", "созвон", "звонок", "позвонить")):
        return "💻"
    return "📌"


# ───────────────────────── Подписи дат/времени ──────────────────────────

def format_date(d: date, tz: ZoneInfo) -> str:
    """Дата для вывода: '15.06' или '15.06.2027' если год не текущий."""
    today = datetime.now(tz).date()
    return d.strftime("%d.%m.%Y") if d.year != today.year else d.strftime("%d.%m")


def format_when(dt: datetime, tz: ZoneInfo) -> str:
    """Человеческая подпись момента: 'сегодня в 11:00' / 'завтра ...' / '15.06 в 10:00'."""
    today = datetime.now(tz).date()
    d = dt.date()
    if d == today:
        prefix = "сегодня"
    elif d == today + timedelta(days=1):
        prefix = "завтра"
    else:
        prefix = format_date(d, tz)
    return f"{prefix} в {dt.strftime('%H:%M')}"


def _parse_dt(s: str) -> datetime:
    """Разбирает 'YYYY-MM-DD HH:MM' от Gemini в datetime (без таймзоны — местное время)."""
    s = s.strip()
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M")
    except ValueError:
        return datetime.fromisoformat(s)  # на случай секунд/другого ISO


def _parse_bday(s: str):
    """Разбирает 'MM-DD' от Gemini в (month, day)."""
    month, day = s.strip().split("-")
    return int(month), int(day)


# ───────────────────────── Поиск задачи по описанию (для delete/reschedule) ──────────

# Слова, которые в описании «что удалить/перенести» не относятся к названию задачи.
MATCH_STOP_WORDS = {
    "задачу", "задача", "задание", "напоминание", "напоминалку", "напоминалка",
    "дело", "из", "списка", "список", "это", "эту", "этот", "напоминалки",
}


def find_matches(query: str, user_id: int):
    """Ищет активные напоминания пользователя, чьё название похоже на описание."""
    qtokens = [
        w for w in re.findall(r"[а-яёa-z0-9]+", query.lower())
        if w not in MATCH_STOP_WORDS and len(w) >= 3
    ]
    if not qtokens:
        return []

    matches = []
    for r in get_active_reminders(user_id):
        ttokens = re.findall(r"[а-яёa-z0-9]+", r["text"].lower())
        # совпадение по началу слова (4 буквы) — ловит «зал», «встречу»~«встреча» и т.п.
        if any(any(tt.startswith(qt[:4]) or qt.startswith(tt[:4]) for tt in ttokens) for qt in qtokens):
            matches.append(r)
    return matches


def _match_label(r) -> str:
    """Подпись задачи для кнопки выбора при удалении."""
    if r["recurrence"] == "yearly":
        d = datetime.fromisoformat(r["remind_at"]).date()
        return f"🎂 {ru_day_month(d)} {r['text']}"
    dt = datetime.fromisoformat(r["remind_at"])
    return f"{dt:%d.%m %H:%M} {r['text']}"


# ───────────────────────── Выполнение действий от Gemini ──────────────────────────

def _do_task(context, action: dict, user_id: int, tz: ZoneInfo) -> str:
    """Добавляет разовую задачу. Возвращает строку отчёта."""
    text = (action.get("text") or "").strip()
    dt_str = action.get("datetime")
    if not text or not dt_str:
        return "⚠️ Пропустил задачу — не понял текст или время."
    try:
        dt = _parse_dt(dt_str)
    except (ValueError, TypeError):
        return f"⚠️ Пропустил «{text}» — не понял время."

    remind_at = dt.strftime("%Y-%m-%d %H:%M")
    rid = add_reminder(user_id, text, remind_at, "none")
    scheduled = schedule_reminder(context.bot, user_id, rid, text, remind_at, "none", tz=tz)

    line = f"✅ Добавил: {pick_emoji(text)} {text} — {format_when(dt, tz)}"
    if not scheduled:
        line += " <i>(время уже прошло — напоминания не будет)</i>"
    return line


def _do_birthday(context, action: dict, user_id: int, tz: ZoneInfo) -> str:
    """Запоминает день рождения (ежегодно). Возвращает строку отчёта."""
    name = (action.get("name") or action.get("text") or "").strip()
    date_str = action.get("date")
    if not name or not date_str:
        return "⚠️ Пропустил день рождения — не понял имя или дату."
    try:
        month, day = _parse_bday(date_str)
        d = date(2024, month, day)  # 2024 — високосный, чтобы 29 февраля жило
    except (ValueError, TypeError):
        return f"⚠️ Пропустил ДР «{name}» — не понял дату."

    remind_at = f"2024-{month:02d}-{day:02d} 09:00"
    rid = add_reminder(user_id, name, remind_at, "yearly")
    schedule_reminder(context.bot, user_id, rid, name, remind_at, "yearly", tz=tz)
    return f"✅ Запомнил ДР: 🎂 {name} — {ru_day_month(d)} <i>(напомню за 2 дня и утром)</i>"


def _do_delete(action: dict, user_id: int, pending_buttons: list) -> str:
    """Удаляет задачу по описанию. Несколько похожих → кнопки в pending_buttons."""
    query = (action.get("text") or "").strip()
    matches = find_matches(query, user_id)
    if not matches:
        return f"🤷 Не нашёл, что удалить: «{query}»"
    if len(matches) == 1:
        r = matches[0]
        delete_reminder(user_id, r["id"])
        unschedule_reminder(r["id"])
        return f"🗑 Удалил: {r['text']}"
    items = [("list", r["id"], _match_label(r)) for r in matches]
    pending_buttons.append((f"❓ Несколько похожих на «{query}» — что удалить?", items))
    return None  # отчёт не нужен, будут кнопки


def _do_reschedule(context, action: dict, user_id: int, tz: ZoneInfo) -> str:
    """Переносит существующую задачу на новое время. Возвращает строку отчёта."""
    query = (action.get("text") or "").strip()
    dt_str = action.get("datetime")
    if not dt_str:
        return f"⚠️ Не понял, на когда переносить «{query}»."
    try:
        dt = _parse_dt(dt_str)
    except (ValueError, TypeError):
        return f"⚠️ Не понял новое время для «{query}»."

    matches = find_matches(query, user_id)
    if not matches:
        return f"🤷 Не нашёл задачу для переноса: «{query}»"
    if len(matches) > 1:
        return f"❓ Несколько задач похожи на «{query}» — уточни, какую перенести."

    r = matches[0]
    remind_at = dt.strftime("%Y-%m-%d %H:%M")
    update_reminder_time(user_id, r["id"], remind_at)
    unschedule_reminder(r["id"])
    schedule_reminder(context.bot, user_id, r["id"], r["text"], remind_at, r["recurrence"], tz=tz)
    return f"🔄 Перенёс: {pick_emoji(r['text'])} {r['text']} → {format_when(dt, tz)}"


# Ожидающие подтверждения разборы: token → {"user_id", "actions"}.
# В памяти (теряется при рестарте — это нормально, подтверждение транзиентное).
# Небольшая утечка, если юзер не жмёт кнопки; для личного бота некритично.
_PENDING_CONFIRM = {}


def _execute_actions(context, actions: list, user_id: int, tz: ZoneInfo):
    """
    Выполняет список действий (task/birthday/delete/reschedule).
    Возвращает (report_lines, pending_buttons) — строки отчёта и неоднозначные
    удаления, требующие выбора кнопкой.
    """
    report = []
    pending_buttons = []
    for action in actions:
        kind = action.get("type")
        if kind == "task":
            report.append(_do_task(context, action, user_id, tz))
        elif kind == "birthday":
            report.append(_do_birthday(context, action, user_id, tz))
        elif kind == "delete":
            line = _do_delete(action, user_id, pending_buttons)
            if line:
                report.append(line)
        elif kind == "reschedule":
            report.append(_do_reschedule(context, action, user_id, tz))
    return report, pending_buttons


def _preview_actions(actions: list, user_id: int, tz: ZoneInfo) -> str:
    """
    Человекочитаемое превью того, что бот СОБИРАЕТСЯ сделать — БЕЗ изменения базы.
    Для delete/reschedule использует read-only find_matches, чтобы показать цель.
    """
    lines = []
    for a in actions:
        kind = a.get("type")
        if kind == "task":
            text = (a.get("text") or "").strip()
            try:
                dt = _parse_dt(a.get("datetime"))
                lines.append(f"➕ {pick_emoji(text)} {text} — {format_when(dt, tz)}")
            except (ValueError, TypeError):
                lines.append(f"⚠️ задача «{text}» — не понял время")
        elif kind == "birthday":
            name = (a.get("name") or a.get("text") or "").strip()
            try:
                month, day = _parse_bday(a.get("date"))
                lines.append(f"🎂 ДР {name} — {ru_day_month(date(2024, month, day))}")
            except (ValueError, TypeError):
                lines.append(f"⚠️ ДР «{name}» — не понял дату")
        elif kind == "delete":
            q = (a.get("text") or "").strip()
            matches = find_matches(q, user_id)
            if not matches:
                lines.append(f"🤷 удалить «{q}» — не нашёл")
            elif len(matches) == 1:
                lines.append(f"🗑 удалить: {matches[0]['text']}")
            else:
                lines.append(f"🗑 удалить «{q}» — несколько, выберу при подтверждении")
        elif kind == "reschedule":
            q = (a.get("text") or "").strip()
            try:
                when = format_when(_parse_dt(a.get("datetime")), tz)
            except (ValueError, TypeError):
                when = "?"
            matches = find_matches(q, user_id)
            if not matches:
                lines.append(f"🤷 перенести «{q}» — не нашёл")
            elif len(matches) == 1:
                lines.append(f"🔄 перенести: {matches[0]['text']} → {when}")
            else:
                lines.append(f"🔄 перенести «{q}» — несколько похожих")

    body = "\n".join(f"• {line}" for line in lines)
    return f"🤔 Понял так — всё верно?\n\n{body}"


async def _send_results(send, report, pending_buttons):
    """Отправляет отчёт (с меню) и сообщения выбора для неоднозначных удалений.

    send — корутина-отправитель вида send(text, **kwargs) (reply или bot.send_message)."""
    if report:
        await send("\n".join(report), parse_mode="HTML", reply_markup=build_menu())
    for prompt, items in pending_buttons:
        await send(prompt, reply_markup=_item_keyboard(items, with_done=False))


async def process_free_text(update: Update, context, raw_text: str):
    """
    Единый обработчик текста и голоса: отдаёт фразу Gemini (в таймзоне юзера),
    получает список действий. Сложные/рисковые (несколько действий или
    удаление/перенос) сначала показываем на подтверждение; одиночное добавление —
    сразу.
    """
    user_id = update.effective_user.id
    tz = _tz(user_id)

    # Разбор — блокирующий сетевой вызов, уносим в поток, чтобы не подвешивать бота.
    try:
        actions = await asyncio.to_thread(brain.parse_message, raw_text, tz)
    except Exception as e:
        print(f"[process_free_text] ошибка разбора: {e!r}")  # видно в логах Railway
        await update.message.reply_text("⚠️ Не понял, переформулируй")
        return

    if not actions:
        await update.message.reply_text("⚠️ Не понял, переформулируй")
        return

    # Подтверждение нужно, если действий несколько ИЛИ есть удаление/перенос.
    needs_confirm = len(actions) > 1 or any(
        a.get("type") in ("delete", "reschedule") for a in actions
    )

    if needs_confirm:
        token = secrets.token_urlsafe(6)
        _PENDING_CONFIRM[token] = {"user_id": user_id, "actions": actions}
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Принять", callback_data=f"ok:{token}"),
            InlineKeyboardButton("↩️ Отменить", callback_data=f"no:{token}"),
        ]])
        await update.message.reply_text(
            _preview_actions(actions, user_id, tz), parse_mode="HTML", reply_markup=keyboard
        )
        return

    # Простой случай (одиночное добавление) — выполняем сразу.
    report, pending_buttons = _execute_actions(context, actions, user_id, tz)
    await _send_results(update.message.reply_text, report, pending_buttons)


# ───────────────────────── Обработчики команд ──────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return

    ensure_user(update.effective_user.id)  # заводим пользователя с таймзоной по умолчанию

    await update.message.reply_text(
        "Привет! Я твой личный планер. ⏱ Время — по Бали (UTC+8).\n\n"
        "Просто напиши или скажи 🎤 что угодно — я сам пойму:\n"
        "<i>завтра в 15:00 встреча с клиентом</i>\n"
        "<i>позвонить маме через 2 часа</i>\n"
        "<i>15.06 в 10:00 оплатить аренду</i>\n"
        "<i>ДР мамы 20 августа</i>\n"
        "<i>перенеси зал на 18:00</i>\n"
        "<i>удали созвон</i>\n\n"
        "Можно несколько дел в одной фразе — разберу все сразу.\n\n"
        "Команды:\n"
        "/today — задачи на сегодня\n"
        "/week — задачи на 7 дней вперёд\n"
        "/birthdays — дни рождения\n"
        "/add 15:00 текст — задача на сегодня\n"
        "/done 3 — отметить выполненной",
        parse_mode="HTML",
        reply_markup=build_menu(),   # показываем нижнее меню
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Не хватает аргументов.\n"
            "Формат: /add 15:00 текст напоминания"
        )
        return

    user_id = update.effective_user.id
    tz = _tz(user_id)
    time_str = context.args[0]
    text = " ".join(context.args[1:])

    try:
        datetime.strptime(time_str, "%H:%M")
    except ValueError:
        await update.message.reply_text(
            "Неверный формат времени. Нужно ЧЧ:ММ, например: /add 09:30 позвонить"
        )
        return

    # /add ставит задачу на сегодня в указанное время.
    today = datetime.now(tz).date()
    remind_at = f"{today.isoformat()} {time_str}"
    reminder_id = add_reminder(user_id, text, remind_at)
    scheduled = schedule_reminder(context.bot, user_id, reminder_id, text, remind_at, tz=tz)

    if scheduled:
        await update.message.reply_text(f"✅ Добавлено: [{time_str}] {text}")
    else:
        await update.message.reply_text(
            f"Сохранено, но время {time_str} уже прошло — сегодня напоминания не будет."
        )


# ───────────────────────── Вывод: названия и форматирование ──────────────────────────

RU_WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
RU_MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def ru_day_month(d: date) -> str:
    """Дата словами: '9 июня'."""
    return f"{d.day} {RU_MONTHS_GEN[d.month - 1]}"


def ru_date_header(d: date) -> str:
    """Заголовок дня: 'Понедельник, 9 июня'."""
    return f"{RU_WEEKDAYS[d.weekday()]}, {ru_day_month(d)}"


def days_word(n: int) -> str:
    """Склонение слова «день» под число: 1 день, 2 дня, 5 дней."""
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return "день"
    if 2 <= n10 <= 4 and not (12 <= n100 <= 14):
        return "дня"
    return "дней"


def _short(s: str, n: int = 25) -> str:
    """Обрезает подпись кнопки, чтобы не была слишком длинной."""
    return s if len(s) <= n else s[: n - 1] + "…"


def _item_keyboard(items, with_done: bool = True):
    """
    items — список (view, id, подпись). На каждую задачу строка кнопок:
    с галочкой «✅ выполнено» (основное действие) и 🗑 удалить.
    with_done=False — только 🗑 (для дней рождения и выбора при удалении).
    """
    if not items:
        return None
    rows = []
    for view, rid, label in items:
        if with_done:
            rows.append([
                InlineKeyboardButton(f"✅ {_short(label)}", callback_data=f"done:{view}:{rid}"),
                InlineKeyboardButton("🗑", callback_data=f"del:{view}:{rid}"),
            ])
        else:
            rows.append([
                InlineKeyboardButton(f"🗑 {_short(label)}", callback_data=f"del:{view}:{rid}"),
            ])
    return InlineKeyboardMarkup(rows)


def _next_birthday(r, today: date) -> date:
    """Ближайшая будущая дата ДР (в этом году или в следующем)."""
    d = datetime.fromisoformat(r["remind_at"]).date()
    for year in (today.year, today.year + 1):
        try:
            nxt = date(year, d.month, d.day)
        except ValueError:           # 29 февраля в невисокосный год → 1 марта
            nxt = date(year, 3, 1)
        if nxt >= today:
            return nxt
    return today


def render_today(user_id: int, tz: ZoneInfo):
    today = datetime.now(tz).date()
    rows = [
        r for r in get_active_reminders(user_id)
        if r["recurrence"] == "none" and datetime.fromisoformat(r["remind_at"]).date() == today
    ]
    if not rows:
        return "✨ На сегодня пусто", None

    rows.sort(key=lambda r: r["remind_at"])  # ISO-строки сортируются по времени корректно
    blocks, items = [], []
    for i, r in enumerate(rows, 1):
        dt = datetime.fromisoformat(r["remind_at"])
        # Блок задачи: «1. 🏋 Тренировка» + время на отдельной строке.
        blocks.append(f"{i}. {pick_emoji(r['text'])} {r['text']}\n   🕐 <code>{dt:%H:%M}</code>")
        items.append(("today", r["id"], f"{dt:%H:%M} {r['text']}"))
    body = "\n\n".join(blocks)
    return f"📋 <b>Задачи на сегодня:</b>\n\n{body}", _item_keyboard(items)


def render_week(user_id: int, tz: ZoneInfo):
    today = datetime.now(tz).date()
    end = today + timedelta(days=6)  # сегодня + 6 дней = 7 дней всего
    rows = [
        r for r in get_active_reminders(user_id)
        if r["recurrence"] == "none"
        and today <= datetime.fromisoformat(r["remind_at"]).date() <= end
    ]
    if not rows:
        return "✨ На ближайшую неделю задач нет", None

    rows.sort(key=lambda r: r["remind_at"])
    parts, items = [], []
    current_day, day_lines = None, []
    for r in rows:
        dt = datetime.fromisoformat(r["remind_at"])
        d = dt.date()
        if d != current_day:                       # начался новый день → новый блок
            if day_lines:
                parts.append("\n".join(day_lines))
            day_lines = [f"📅 <b>{ru_date_header(d)}</b>"]
            current_day = d
        day_lines.append(f"{pick_emoji(r['text'])} {r['text']}")
        day_lines.append(f"   🕐 <code>{dt:%H:%M}</code>")
        items.append(("week", r["id"], f"{dt:%d.%m %H:%M} {r['text']}"))
    if day_lines:
        parts.append("\n".join(day_lines))

    body = "\n\n".join(parts)  # пустая строка между днями
    return f"🗓 <b>Задачи на неделю:</b>\n\n{body}", _item_keyboard(items)


def render_list(user_id: int, tz: ZoneInfo):
    reminders = [r for r in get_active_reminders(user_id) if r["recurrence"] == "none"]
    if not reminders:
        return "✨ Задач нет. Просто напиши, что и когда напомнить.", None

    reminders.sort(key=lambda r: r["remind_at"])
    blocks, items = [], []
    for i, r in enumerate(reminders, 1):
        dt = datetime.fromisoformat(r["remind_at"])
        blocks.append(f"{i}. {pick_emoji(r['text'])} {r['text']}\n   📅 <code>{format_when(dt, tz)}</code>")
        items.append(("list", r["id"], f"{dt:%d.%m %H:%M} {r['text']}"))
    body = "\n\n".join(blocks)
    return f"📋 <b>Все задачи:</b>\n\n{body}", _item_keyboard(items)


def render_birthdays(user_id: int, tz: ZoneInfo):
    bdays = [r for r in get_active_reminders(user_id) if r["recurrence"] == "yearly"]
    if not bdays:
        return "🎂 Дней рождения пока нет.\nДобавь, например: ДР мамы 20 августа", None

    today = datetime.now(tz).date()
    bdays.sort(key=lambda r: _next_birthday(r, today))  # ближайший — сверху

    blocks, items = [], []
    for i, r in enumerate(bdays, 1):
        d = datetime.fromisoformat(r["remind_at"]).date()
        suffix = ""
        if i == 1:  # «через N дней» считаем только до ближайшего
            days = (_next_birthday(r, today) - today).days
            if days == 0:
                suffix = " — сегодня! 🎉"
            elif days == 1:
                suffix = " — завтра"
            else:
                suffix = f" — через {days} {days_word(days)}"
        blocks.append(f"{i}. 🎂 {r['text']}\n   📅 <code>{ru_day_month(d)}</code>{suffix}")
        items.append(("bday", r["id"], f"{ru_day_month(d)} {r['text']}"))
    body = "\n\n".join(blocks)
    return f"🎂 <b>Дни рождения:</b>\n\n{body}", _item_keyboard(items, with_done=False)


# Сопоставление имени вида с его рендером — нужно при перерисовке после удаления.
RENDERERS = {
    "today": render_today, "week": render_week,
    "list": render_list, "bday": render_birthdays,
}

# Кнопка нижнего меню → какой список показать.
MENU_RENDERERS = {
    MENU_TODAY: render_today, MENU_WEEK: render_week,
    MENU_BDAY: render_birthdays, MENU_LIST: render_list,
}


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    uid = update.effective_user.id
    text, markup = render_today(uid, _tz(uid))
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    uid = update.effective_user.id
    text, markup = render_week(uid, _tz(uid))
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    uid = update.effective_user.id
    text, markup = render_list(uid, _tz(uid))
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def cmd_birthdays(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    uid = update.effective_user.id
    text, markup = render_birthdays(uid, _tz(uid))
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def on_delete_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Нажата кнопка 🗑: удаляем задачу и перерисовываем тот же список."""
    query = update.callback_query
    uid = update.effective_user.id
    if uid != MY_CHAT_ID:
        await query.answer()
        return

    _, view, rid = query.data.split(":")
    rid = int(rid)
    row = get_reminder_by_id(uid, rid)

    delete_reminder(uid, rid)
    unschedule_reminder(rid)

    await query.answer("Удалено 🗑" if row else "Уже удалено")

    # Перерисовываем тот же список без удалённой задачи.
    render = RENDERERS.get(view, render_list)
    text, markup = render(uid, _tz(uid))
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)


async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Нажато «✅ Принять» под превью: выполняем отложенные действия."""
    query = update.callback_query
    uid = update.effective_user.id
    if uid != MY_CHAT_ID:
        await query.answer()
        return

    _, token = query.data.split(":")
    pending = _PENDING_CONFIRM.pop(token, None)
    if not pending or pending["user_id"] != uid:
        await query.answer("Уже неактуально")
        return

    tz = _tz(uid)
    report, pending_buttons = _execute_actions(context, pending["actions"], uid, tz)
    await query.answer("Сохранено ✅")

    summary = "✅ Сохранено:\n" + "\n".join(report) if report else "✅ Готово"
    await query.edit_message_text(summary, parse_mode="HTML")

    # Неоднозначные удаления — отдельными сообщениями с кнопками выбора.
    for prompt, items in pending_buttons:
        await context.bot.send_message(uid, prompt, reply_markup=_item_keyboard(items, with_done=False))


async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Нажато «↩️ Отменить» под превью: ничего не сохраняем."""
    query = update.callback_query
    uid = update.effective_user.id
    if uid != MY_CHAT_ID:
        await query.answer()
        return

    _, token = query.data.split(":")
    _PENDING_CONFIRM.pop(token, None)
    await query.answer("Отменено")
    await query.edit_message_text("↩️ Отменено — ничего не сохранил.")


async def on_snooze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Нажато ⏰ под напоминанием: переносим задачу на +N минут от текущего момента."""
    query = update.callback_query
    uid = update.effective_user.id
    if uid != MY_CHAT_ID:
        await query.answer()
        return

    _, rid, minutes = query.data.split(":")
    rid, minutes = int(rid), int(minutes)
    tz = _tz(uid)

    r = get_reminder_by_id(uid, rid)
    if not r:
        await query.answer("Задачи уже нет")
        return

    new_dt = datetime.now(tz) + timedelta(minutes=minutes)
    remind_at = new_dt.strftime("%Y-%m-%d %H:%M")
    update_reminder_time(uid, rid, remind_at)
    unschedule_reminder(rid)
    schedule_reminder(context.bot, uid, rid, r["text"], remind_at, r["recurrence"], tz=tz)

    await query.answer(f"Отложил на {minutes} мин")
    await query.edit_message_text(f"⏰ Отложено до {new_dt:%H:%M}: {r['text']}")


async def on_done_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Нажата кнопка ✅: отмечаем выполненной, показываем счётчик за день, перерисовываем."""
    query = update.callback_query
    uid = update.effective_user.id
    if uid != MY_CHAT_ID:
        await query.answer()
        return

    _, view, rid = query.data.split(":")
    rid = int(rid)
    tz = _tz(uid)
    now = datetime.now(tz)

    ok = mark_done(uid, rid, now.strftime("%Y-%m-%d %H:%M"))
    unschedule_reminder(rid)  # снимаем запланированные напоминания закрытой задачи

    done_today = count_done_on(uid, now.date().isoformat()) if ok else None
    await query.answer(f"Готово ✅  ({done_today} за сегодня)" if ok else "Уже закрыто")

    # Нажато прямо из напоминания (не из списка) — просто отмечаем сообщение.
    if view == "remind":
        suffix = f"  ({done_today} за сегодня)" if ok else ""
        await query.edit_message_text(f"✅ Выполнено{suffix}")
        return

    # Иначе это список — перерисовываем его без закрытой задачи.
    render = RENDERERS.get(view, render_list)
    text, markup = render(uid, tz)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return

    if len(context.args) != 1:
        await update.message.reply_text("Формат: /done <номер>, например /done 3")
        return

    try:
        reminder_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Номер должен быть числом, например: /done 3")
        return

    uid = update.effective_user.id
    now = datetime.now(_tz(uid))
    success = mark_done(uid, reminder_id, now.strftime("%Y-%m-%d %H:%M"))
    if success:
        done_today = count_done_on(uid, now.date().isoformat())
        await update.message.reply_text(
            f"✅ Напоминание #{reminder_id} выполнено!  ({done_today} за сегодня)"
        )
    else:
        await update.message.reply_text(
            f"Напоминание #{reminder_id} не найдено или уже выполнено."
        )


# ───────────────────────── Свободный текст и голос ──────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принимает обычный текст. Сначала проверяем кнопки меню, потом — разбор через Gemini."""
    if not is_owner(update):
        return

    text = update.message.text
    uid = update.effective_user.id

    # Нажата кнопка нижнего меню — показываем нужный список, не трогая Gemini.
    if text in MENU_RENDERERS:
        render = MENU_RENDERERS[text]
        out, markup = render(uid, _tz(uid))
        await update.message.reply_text(out, parse_mode="HTML", reply_markup=markup)
        return

    if text == MENU_ADD:
        await update.message.reply_text(
            "➕ Просто напиши или надиктуй 🎤 задачу — например:\n"
            "<i>завтра в 15 зал</i>. Можно несколько дел в одной фразе.",
            parse_mode="HTML", reply_markup=build_menu(),
        )
        return

    await process_free_text(update, context, text)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Принимает голосовое сообщение:
    скачивает .ogg → распознаёт через stt.transcribe (Groq/Whisper) → отдаёт в Gemini.
    """
    if not is_owner(update):
        return

    await update.message.reply_text("🎤 Слушаю...")

    # Скачиваем голосовой файл во временную директорию
    voice_file = await context.bot.get_file(update.message.voice.file_id)

    with tempfile.TemporaryDirectory() as tmpdir:
        ogg_path = os.path.join(tmpdir, "voice.ogg")
        await voice_file.download_to_drive(ogg_path)

        # Распознаём речь. transcribe — блокирующий вызов (сеть/CPU),
        # поэтому уносим его в отдельный поток, чтобы не подвешивать бота.
        try:
            text = await asyncio.to_thread(transcribe, ogg_path)
        except Exception:
            await update.message.reply_text(
                "Не смог разобрать голос. Попробуй ещё раз или напиши текстом."
            )
            return

    if not text:
        await update.message.reply_text(
            "Не смог разобрать голос. Попробуй ещё раз или напиши текстом."
        )
        return

    # Показываем что распознали, потом обрабатываем как обычный текст
    await update.message.reply_text(f"🎤 Распознал: <i>{text}</i>", parse_mode="HTML")
    await process_free_text(update, context, text)


# ───────────────────────── Запуск ──────────────────────────

async def on_startup(application: Application):
    scheduler.start()
    reminders = get_all_active_reminders()       # напоминания всех пользователей
    reschedule_all(application.bot, reminders)
    print(f"Восстановлено напоминаний из базы: {len(reminders)}")


if __name__ == "__main__":
    init_db(MY_CHAT_ID)  # передаём владельца: старые задачи привяжутся к нему

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
        .build()
    )

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add",   cmd_add))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("week",  cmd_week))
    app.add_handler(CommandHandler("list",  cmd_list))
    app.add_handler(CommandHandler("birthdays", cmd_birthdays))
    app.add_handler(CommandHandler("done",  cmd_done))

    # Нажатия кнопок: подтверждение разбора, снуз, ✅ выполнено, 🗑 удаление
    app.add_handler(CallbackQueryHandler(on_confirm, pattern=r"^ok:"))
    app.add_handler(CallbackQueryHandler(on_cancel, pattern=r"^no:"))
    app.add_handler(CallbackQueryHandler(on_snooze, pattern=r"^snooze:"))
    app.add_handler(CallbackQueryHandler(on_done_button, pattern=r"^done:"))
    app.add_handler(CallbackQueryHandler(on_delete_button, pattern=r"^del:"))

    # Свободный текст и голос — после команд, чтобы не перехватывать /команды
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    print("Бот запущен. Нажми Ctrl+C чтобы остановить.")
    app.run_polling()
