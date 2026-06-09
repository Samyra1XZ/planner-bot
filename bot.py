import asyncio
import os
import re
import tempfile
from datetime import datetime, timedelta, date

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler, CallbackQueryHandler, filters,
)

from database import (
    init_db, add_reminder, get_active_reminders, mark_done,
    delete_reminder, get_reminder_by_id, update_reminder_time,
)
from scheduler import scheduler, schedule_reminder, reschedule_all, unschedule_reminder, TZ
from stt import transcribe
import brain  # «мозг» разбора на Gemini

# Загружаем переменные из файла .env (BOT_TOKEN, MY_CHAT_ID)
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
MY_CHAT_ID = int(os.getenv("MY_CHAT_ID"))


def is_owner(update: Update) -> bool:
    """Проверяет, что команду отправил именно я, а не кто-то чужой."""
    return update.effective_user.id == MY_CHAT_ID


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

def format_date(d: date) -> str:
    """Дата для вывода: '15.06' или '15.06.2027' если год не текущий."""
    today = datetime.now(TZ).date()
    return d.strftime("%d.%m.%Y") if d.year != today.year else d.strftime("%d.%m")


def format_when(dt: datetime) -> str:
    """Человеческая подпись момента: 'сегодня в 11:00' / 'завтра ...' / '15.06 в 10:00'."""
    today = datetime.now(TZ).date()
    d = dt.date()
    if d == today:
        prefix = "сегодня"
    elif d == today + timedelta(days=1):
        prefix = "завтра"
    else:
        prefix = format_date(d)
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


def find_matches(query: str):
    """Ищет активные напоминания, чьё название похоже на описание из действия."""
    qtokens = [
        w for w in re.findall(r"[а-яёa-z0-9]+", query.lower())
        if w not in MATCH_STOP_WORDS and len(w) >= 3
    ]
    if not qtokens:
        return []

    matches = []
    for r in get_active_reminders():
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

def _do_task(context, action: dict) -> str:
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
    rid = add_reminder(text, remind_at, "none")
    scheduled = schedule_reminder(context.bot, MY_CHAT_ID, rid, text, remind_at, "none")

    line = f"✅ Добавил: {pick_emoji(text)} {text} — {format_when(dt)}"
    if not scheduled:
        line += " <i>(время уже прошло — напоминания не будет)</i>"
    return line


def _do_birthday(context, action: dict) -> str:
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
    rid = add_reminder(name, remind_at, "yearly")
    schedule_reminder(context.bot, MY_CHAT_ID, rid, name, remind_at, "yearly")
    return f"✅ Запомнил ДР: 🎂 {name} — {ru_day_month(d)} <i>(напомню за 2 дня и утром)</i>"


def _do_delete(action: dict, pending_buttons: list) -> str:
    """Удаляет задачу по описанию. Несколько похожих → кладёт кнопки в pending_buttons."""
    query = (action.get("text") or "").strip()
    matches = find_matches(query)
    if not matches:
        return f"🤷 Не нашёл, что удалить: «{query}»"
    if len(matches) == 1:
        r = matches[0]
        delete_reminder(r["id"])
        unschedule_reminder(r["id"])
        return f"🗑 Удалил: {r['text']}"
    items = [("list", r["id"], _match_label(r)) for r in matches]
    pending_buttons.append((f"❓ Несколько похожих на «{query}» — что удалить?", items))
    return None  # отчёт не нужен, будут кнопки


def _do_reschedule(context, action: dict) -> str:
    """Переносит существующую задачу на новое время. Возвращает строку отчёта."""
    query = (action.get("text") or "").strip()
    dt_str = action.get("datetime")
    if not dt_str:
        return f"⚠️ Не понял, на когда переносить «{query}»."
    try:
        dt = _parse_dt(dt_str)
    except (ValueError, TypeError):
        return f"⚠️ Не понял новое время для «{query}»."

    matches = find_matches(query)
    if not matches:
        return f"🤷 Не нашёл задачу для переноса: «{query}»"
    if len(matches) > 1:
        return f"❓ Несколько задач похожи на «{query}» — уточни, какую перенести."

    r = matches[0]
    remind_at = dt.strftime("%Y-%m-%d %H:%M")
    update_reminder_time(r["id"], remind_at)
    unschedule_reminder(r["id"])
    schedule_reminder(context.bot, MY_CHAT_ID, r["id"], r["text"], remind_at, r["recurrence"])
    return f"🔄 Перенёс: {pick_emoji(r['text'])} {r['text']} → {format_when(dt)}"


async def process_free_text(update: Update, context, raw_text: str):
    """
    Единый обработчик текста и голоса: отдаёт фразу Gemini, получает список
    действий (task/birthday/delete/reschedule) и выполняет их, затем шлёт отчёт.
    """
    # Разбор — блокирующий сетевой вызов, уносим в поток, чтобы не подвешивать бота.
    try:
        actions = await asyncio.to_thread(brain.parse_message, raw_text)
    except Exception:
        await update.message.reply_text("⚠️ Не понял, переформулируй")
        return

    if not actions:
        await update.message.reply_text("⚠️ Не понял, переформулируй")
        return

    report = []                 # строки отчёта о выполненном
    pending_buttons = []        # (текст, items) для неоднозначных удалений

    for action in actions:
        kind = action.get("type")
        if kind == "task":
            report.append(_do_task(context, action))
        elif kind == "birthday":
            report.append(_do_birthday(context, action))
        elif kind == "delete":
            line = _do_delete(action, pending_buttons)
            if line:
                report.append(line)
        elif kind == "reschedule":
            report.append(_do_reschedule(context, action))

    if report:
        await update.message.reply_text("\n".join(report), parse_mode="HTML")

    # Неоднозначные удаления — отдельными сообщениями с кнопками выбора.
    for prompt, items in pending_buttons:
        await update.message.reply_text(prompt, reply_markup=_delete_keyboard(items))


# ───────────────────────── Обработчики команд ──────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return

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
    today = datetime.now(TZ).date()
    remind_at = f"{today.isoformat()} {time_str}"
    reminder_id = add_reminder(text, remind_at)
    scheduled = schedule_reminder(context.bot, MY_CHAT_ID, reminder_id, text, remind_at)

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


def _delete_keyboard(items):
    """items — список (view, id, подпись). Делаем по кнопке 🗑 на строку."""
    if not items:
        return None
    rows = [
        [InlineKeyboardButton(f"🗑 {_short(label)}", callback_data=f"del:{view}:{rid}")]
        for view, rid, label in items
    ]
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


def render_today():
    today = datetime.now(TZ).date()
    rows = [
        r for r in get_active_reminders()
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
    return f"📋 <b>Задачи на сегодня:</b>\n\n{body}", _delete_keyboard(items)


def render_week():
    today = datetime.now(TZ).date()
    end = today + timedelta(days=6)  # сегодня + 6 дней = 7 дней всего
    rows = [
        r for r in get_active_reminders()
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
    return f"🗓 <b>Задачи на неделю:</b>\n\n{body}", _delete_keyboard(items)


def render_list():
    reminders = [r for r in get_active_reminders() if r["recurrence"] == "none"]
    if not reminders:
        return "✨ Задач нет. Просто напиши, что и когда напомнить.", None

    reminders.sort(key=lambda r: r["remind_at"])
    blocks, items = [], []
    for i, r in enumerate(reminders, 1):
        dt = datetime.fromisoformat(r["remind_at"])
        blocks.append(f"{i}. {pick_emoji(r['text'])} {r['text']}\n   📅 <code>{format_when(dt)}</code>")
        items.append(("list", r["id"], f"{dt:%d.%m %H:%M} {r['text']}"))
    body = "\n\n".join(blocks)
    return f"📋 <b>Все задачи:</b>\n\n{body}", _delete_keyboard(items)


def render_birthdays():
    bdays = [r for r in get_active_reminders() if r["recurrence"] == "yearly"]
    if not bdays:
        return "🎂 Дней рождения пока нет.\nДобавь, например: ДР мамы 20 августа", None

    today = datetime.now(TZ).date()
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
    return f"🎂 <b>Дни рождения:</b>\n\n{body}", _delete_keyboard(items)


# Сопоставление имени вида с его рендером — нужно при перерисовке после удаления.
RENDERERS = {
    "today": render_today, "week": render_week,
    "list": render_list, "bday": render_birthdays,
}


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    text, markup = render_today()
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    text, markup = render_week()
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    text, markup = render_list()
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def cmd_birthdays(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    text, markup = render_birthdays()
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def on_delete_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Нажата кнопка 🗑: удаляем задачу и перерисовываем тот же список."""
    query = update.callback_query
    if update.effective_user.id != MY_CHAT_ID:
        await query.answer()
        return

    _, view, rid = query.data.split(":")
    rid = int(rid)
    row = get_reminder_by_id(rid)

    delete_reminder(rid)
    unschedule_reminder(rid)

    await query.answer("Удалено 🗑" if row else "Уже удалено")

    # Перерисовываем тот же список без удалённой задачи.
    render = RENDERERS.get(view, render_list)
    text, markup = render()
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

    success = mark_done(reminder_id)
    if success:
        await update.message.reply_text(f"✅ Напоминание #{reminder_id} выполнено!")
    else:
        await update.message.reply_text(
            f"Напоминание #{reminder_id} не найдено или уже выполнено."
        )


# ───────────────────────── Свободный текст и голос ──────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принимает обычный текст (не команду) и обрабатывает через Gemini."""
    if not is_owner(update):
        return
    await process_free_text(update, context, update.message.text)


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
    reminders = get_active_reminders()
    reschedule_all(application.bot, MY_CHAT_ID, reminders)
    print(f"Восстановлено напоминаний из базы: {len(reminders)}")


if __name__ == "__main__":
    init_db()

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

    # Нажатия кнопок 🗑 удаления
    app.add_handler(CallbackQueryHandler(on_delete_button, pattern=r"^del:"))

    # Свободный текст и голос — после команд, чтобы не перехватывать /команды
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    print("Бот запущен. Нажми Ctrl+C чтобы остановить.")
    app.run_polling()
