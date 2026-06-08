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
    delete_reminder, get_reminder_by_id,
)
from scheduler import scheduler, schedule_reminder, reschedule_all, unschedule_reminder, TZ
from stt import transcribe

# Загружаем переменные из файла .env (BOT_TOKEN, MY_CHAT_ID)
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
MY_CHAT_ID = int(os.getenv("MY_CHAT_ID"))


def is_owner(update: Update) -> bool:
    """Проверяет, что команду отправил именно я, а не кто-то чужой."""
    return update.effective_user.id == MY_CHAT_ID


# ───────────────────────── Вспомогательные функции ──────────────────────────

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


# Служебные слова, которые человек говорит вокруг задачи, но в списке они не нужны.
FILLER_WORDS = {
    "запланируй", "запланировать", "напомни", "напомнить", "поставь", "поставить",
    "добавь", "добавить", "создай", "создать", "запиши", "записать", "сделай",
    "надо", "нужно", "мне", "себе", "пожалуйста", "плиз",
}

# Слова-даты: в названии задачи они не нужны (дату вытащит extract_date отдельно).
DATE_WORDS = {
    "сегодня", "завтра", "послезавтра", "сейчас", "нынче", "утром", "днём", "днем",
    "вечером", "ночью", "понедельник", "вторник", "среду", "четверг", "пятницу",
    "субботу", "воскресенье",
}

# Предлоги, которые отрезаем по краям задачи (в середине не трогаем — «позвонить в банк»).
EDGE_PREPOSITIONS = {"на", "в", "о", "об", "про", "к"}

# Нормализация частых разговорных сокращений до нормального вида.
ABBREVIATIONS = {
    "треню": "тренировка", "треня": "тренировка", "тренька": "тренировка",
    "тренеровка": "тренировка", "трени": "тренировка",
}

# Части суток для пересчёта «7 вечера» → 19:00 и т.п.
def _apply_part_of_day(hour: int, part: str) -> int:
    if part.startswith("веч"):                       # вечера → +12
        return hour + 12 if hour < 12 else hour
    if part.startswith("дн") or part.startswith("дня"):  # дня → +12 (кроме 12)
        return hour + 12 if hour < 12 else hour
    if part.startswith("ноч"):                       # 12 ночи → 0
        return 0 if hour == 12 else hour
    if part.startswith("утр"):                       # 12 утра → 0
        return 0 if hour == 12 else hour
    return hour


def extract_time(text: str):
    """
    Достаёт время из русской фразы. Возвращает (hour, minute, (start, end))
    с координатами найденного куска или None. Регистр не важен (длина строки
    при .lower() не меняется, поэтому координаты подходят и к оригиналу).
    """
    t = text.lower()

    # «через N часов / N минут» — относительное время от текущего момента
    # (\w* доедает окончание слова: час/часа/часов, мин/минут)
    m = re.search(r"через\s+(\d{1,2})\s*час\w*", t)
    if m:
        base = datetime.now(TZ) + timedelta(hours=int(m.group(1)))
        return base.hour, base.minute, m.span()
    m = re.search(r"через\s+(\d{1,3})\s*мин\w*", t)
    if m:
        base = datetime.now(TZ) + timedelta(minutes=int(m.group(1)))
        return base.hour, base.minute, m.span()

    # Явное ЧЧ:ММ или ЧЧ.ММ
    m = re.search(r"\b(\d{1,2})[:.](\d{2})\b", t)
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        return int(m.group(1)), int(m.group(2)), m.span()

    # «N [часов] утра/дня/вечера/ночи» — с частью суток
    m = re.search(r"\b(\d{1,2})\s*(?:час(?:ов|а)?\s*)?(утра|утром|дня|днём|днем|вечера|вечером|ночи|ночью)\b", t)
    if m and int(m.group(1)) <= 23:
        return _apply_part_of_day(int(m.group(1)), m.group(2)) % 24, 0, m.span()

    # «N часов» — 24-часовой формат без части суток
    m = re.search(r"\b(\d{1,2})\s*час(?:ов|а)?\b", t)
    if m and int(m.group(1)) < 24:
        return int(m.group(1)), 0, m.span()

    # «в N» — голое «в 8», «встреча в 9» (трактуем как N:00). Самый общий случай — в конце.
    m = re.search(r"\bв\s+(\d{1,2})\b", t)
    if m and int(m.group(1)) < 24:
        return int(m.group(1)), 0, m.span()

    # Полдень / полночь
    m = re.search(r"полдень|полдня", t)
    if m:
        return 12, 0, m.span()
    m = re.search(r"полноч", t)
    if m:
        return 0, 0, m.span()

    return None


def clean_task_text(text: str) -> str:
    """
    Превращает разговорную фразу в короткую задачу:
    убирает служебные слова, слова-даты и краевые предлоги, нормализует
    сокращения, ставит заглавную букву. 'запланируй на завтра треню' → 'Тренировка'.
    """
    words = []
    for word in text.split():
        bare = word.lower().strip(".,!?;:")
        if bare in FILLER_WORDS or bare in DATE_WORDS:
            continue  # выкидываем «запланируй», «завтра» и т.п.
        words.append(ABBREVIATIONS.get(bare, word))

    # Отрезаем предлоги по краям («на тренировка», «встреча в» → ...).
    while words and words[0].lower().strip(".,!?;:") in EDGE_PREPOSITIONS:
        words.pop(0)
    while words and words[-1].lower().strip(".,!?;:") in EDGE_PREPOSITIONS:
        words.pop()

    cleaned = " ".join(words).strip(" ,.-—")
    if cleaned:
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned


# Месяцы (по основе слова, чтобы ловить «июня», «августа» и т.п.).
# Порядок важен: «март» раньше «май», иначе «марта» уйдёт не туда.
MONTH_STEMS = [
    ("январ", 1), ("феврал", 2), ("март", 3), ("апрел", 4), ("май", 5), ("мая", 5),
    ("июн", 6), ("июл", 7), ("август", 8), ("сентябр", 9), ("октябр", 10),
    ("ноябр", 11), ("декабр", 12),
]

# Дни недели как точные шаблоны (понедельник=0 ... воскресенье=6).
# «сред[ауы]» — чтобы не цеплять «средство» и т.п.
WEEKDAY_PATTERNS = [
    (r"понедельник\w*", 0), (r"вторник\w*", 1), (r"сред[ауы]\b", 2),
    (r"четверг\w*", 3), (r"пятниц\w*", 4), (r"суббот\w*", 5), (r"воскресень\w*", 6),
]

# Признаки дня рождения / годовщины → ежегодное напоминание.
BIRTHDAY_RE = re.compile(r"день\s+рождени\w*|днюх\w*|годовщин\w*|\bдр\b|\bд\.?\s?р\.?", re.IGNORECASE)


def _month_from_word(word: str):
    for stem, num in MONTH_STEMS:
        if word.startswith(stem):
            return num
    return None


def extract_date(text: str):
    """
    Достаёт дату из фразы. Возвращает (datetime.date, (start, end)) или None.
    Понимает: 15.06.2026 / 15.06 / 15 июня [2026] / сегодня / завтра /
    послезавтра / «в пятницу». Год не указан → этот год, а если дата уже
    прошла — следующий (для разовых задач).
    """
    t = text.lower()
    today = datetime.now(TZ).date()

    def resolve_year(day, month, year=None):
        if year is not None:
            year = year + 2000 if year < 100 else year
            return date(year, month, day)
        d = date(today.year, month, day)
        return d if d >= today else date(today.year + 1, month, day)

    # 15.06 или 15.06.2026 (точка/слэш). Месяц 1–12, день 1–31 — иначе это не дата (а время).
    m = re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b", t)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else None
        if 1 <= month <= 12 and 1 <= day <= 31:
            try:
                return resolve_year(day, month, year), m.span()
            except ValueError:
                pass

    # 15 июня [2026]
    m = re.search(r"\b(\d{1,2})\s+([а-я]+)(?:\s+(\d{4}))?\b", t)
    if m:
        month = _month_from_word(m.group(2))
        if month:
            day = int(m.group(1))
            year = int(m.group(3)) if m.group(3) else None
            try:
                return resolve_year(day, month, year), m.span()
            except ValueError:
                pass

    # послезавтра / завтра / сегодня
    m = re.search(r"послезавтра", t)
    if m:
        return today + timedelta(days=2), m.span()
    m = re.search(r"завтра", t)
    if m:
        return today + timedelta(days=1), m.span()
    m = re.search(r"сегодня", t)
    if m:
        return today, m.span()

    # «в пятницу» / «понедельник» → ближайший такой день недели
    for pattern, wd in WEEKDAY_PATTERNS:
        m = re.search(r"\b" + pattern, t)
        if m:
            ahead = (wd - today.weekday()) % 7
            ahead = ahead or 7  # сегодня этот день → берём следующий такой
            return today + timedelta(days=ahead), m.span()

    return None


def _blank(text: str, span) -> str:
    """Заменяет найденный кусок пробелами (длина строки сохраняется)."""
    start, end = span
    return text[:start] + " " * (end - start) + text[end:]


def parse_when(text: str) -> dict:
    """
    Разбирает фразу в структуру:
      {recurrence, date, time, task}
    recurrence: 'none' | 'yearly' (день рождения)
    date: datetime.date | None
    time: (hour, minute) | None
    task: очищенное название
    """
    recurrence = "yearly" if BIRTHDAY_RE.search(text) else "none"

    work = text
    # Сначала дату (чтобы «15.06» не утащил разбор времени), потом время.
    date_found = extract_date(work)
    the_date = None
    if date_found is not None:
        the_date, span = date_found
        work = _blank(work, span)

    time_found = extract_time(work)
    the_time = None
    if time_found is not None:
        hour, minute, span = time_found
        the_time = (hour, minute)
        work = _blank(work, span)

    # Убираем сам маркер «день рождения / ДР» из названия
    for m in BIRTHDAY_RE.finditer(work):
        work = _blank(work, m.span())

    task = clean_task_text(work) or clean_task_text(text)
    return {"recurrence": recurrence, "date": the_date, "time": the_time, "task": task}


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


async def _create_reminder(update, context, task, d: date, hm, recurrence, rolled=False):
    """Сохраняет напоминание в базу, ставит в планировщик и отвечает пользователю."""
    dt = datetime(d.year, d.month, d.day, hm[0], hm[1])
    remind_at = dt.strftime("%Y-%m-%d %H:%M")
    reminder_id = add_reminder(task, remind_at, recurrence)
    scheduled = schedule_reminder(context.bot, MY_CHAT_ID, reminder_id, task, remind_at, recurrence)

    if recurrence == "yearly":
        await update.message.reply_text(
            f"✅ Запомнил день рождения:\n🎂 {task} — {format_date(d)} "
            f"<i>(ежегодно: напомню за 2 дня и утром в день)</i>",
            parse_mode="HTML",
        )
        return

    if scheduled:
        extra = " <i>(перенёс на завтра — сегодня это время уже прошло)</i>" if rolled else ""
        await update.message.reply_text(
            f"✅ Понял! Напомню {format_when(dt)}:\n{pick_emoji(task)} {task}{extra}",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            f"Дата {format_when(dt)} уже прошла — напоминание не поставил."
        )


# ───────────────────────── Удаление по голосу/тексту ──────────────────────────

# Намерение удалить: фраза начинается с такого слова.
DELETE_RE = re.compile(
    r"^\s*(удали(ть)?|удоли(ть)?|убери|убрать|сотри|стереть|отмени(ть)?|снеси|удол\w*)\b",
    re.IGNORECASE,
)

# Слова, которые в запросе на удаление не относятся к названию задачи.
DELETE_STOP_WORDS = {
    "задачу", "задача", "задание", "напоминание", "напоминалку", "напоминалка",
    "дело", "из", "списка", "список", "пожалуйста", "плиз", "это", "эту", "этот",
    "мне", "мой", "мою", "про", "напоминалки",
}


def is_delete_intent(text: str) -> bool:
    return bool(DELETE_RE.match(text))


def find_matches(query: str):
    """Ищет активные напоминания (задачи и ДР), чьё название похоже на запрос."""
    qtokens = [
        w for w in re.findall(r"[а-яёa-z0-9]+", query.lower())
        if w not in DELETE_STOP_WORDS and w not in DATE_WORDS and len(w) >= 3
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


async def handle_delete_request(update: Update, context, raw_text: str):
    """Удаление по фразе: 'удали зал'. Одно совпадение — удаляем, несколько — кнопки выбора."""
    query = DELETE_RE.sub("", raw_text, count=1).strip(" ,.:-—")
    matches = find_matches(query)

    if not matches:
        hint = f" по «{query}»" if query else ""
        await update.message.reply_text(
            f"Не нашёл, что удалить{hint}. Посмотри /today или /week и удали кнопкой 🗑."
        )
        return

    if len(matches) == 1:  # однозначно — удаляем сразу
        r = matches[0]
        delete_reminder(r["id"])
        unschedule_reminder(r["id"])
        await update.message.reply_text(f"🗑 Удалил: {r['text']}")
        return

    # несколько похожих — даём выбрать кнопкой
    items = [("list", r["id"], _match_label(r)) for r in matches]
    await update.message.reply_text(
        "Нашёл несколько — что удалить?",
        reply_markup=_delete_keyboard(items),
    )


async def process_free_text(update: Update, context, raw_text: str):
    """
    Общая логика для текстовых и голосовых сообщений.
    Понимает дату, время, дни рождения и удаление; если дата есть, а времени нет — переспрашивает.
    """
    # 0) Просьба удалить — обрабатываем отдельно (и текстом, и голосом).
    if is_delete_intent(raw_text):
        await handle_delete_request(update, context, raw_text)
        return

    # 1) Если раньше переспросили «во сколько?» — пробуем понять ответ как время.
    pending = context.user_data.get("pending")
    if pending:
        tf = extract_time(raw_text)
        leftover = clean_task_text(_blank(raw_text, tf[2])) if tf else clean_task_text(raw_text)
        if tf and not leftover:  # это действительно просто время — достраиваем задачу
            context.user_data.pop("pending")
            d = date.fromisoformat(pending["date"])
            await _create_reminder(update, context, pending["task"], d, (tf[0], tf[1]), "none")
            return
        context.user_data.pop("pending")  # иначе это новая фраза — забываем и разбираем заново

    parsed = parse_when(raw_text)
    recurrence, the_date, the_time, task = (
        parsed["recurrence"], parsed["date"], parsed["time"], parsed["task"],
    )

    # 2) День рождения — нужна дата (день и месяц), время по умолчанию 09:00.
    if recurrence == "yearly":
        if the_date is None:
            await update.message.reply_text(
                "🎂 Не понял дату дня рождения. Например:\n<i>ДР мамы 20 августа</i>",
                parse_mode="HTML",
            )
            return
        await _create_reminder(update, context, task, the_date, the_time or (9, 0), "yearly")
        return

    # 3) Обычная задача
    if the_date is None and the_time is None:
        await update.message.reply_text(
            "⏰ Не понял когда. Например:\n"
            "<i>завтра в 15:00 встреча</i>  или  <i>15.06 в 10:00 оплатить аренду</i>",
            parse_mode="HTML",
        )
        return

    if the_time is None:  # дата есть, времени нет → переспрашиваем
        context.user_data["pending"] = {"date": the_date.isoformat(), "task": task}
        await update.message.reply_text(
            f"🕐 Во сколько напомнить <b>{format_date(the_date)}</b>? "
            f"Напиши время, например <i>11:00</i>",
            parse_mode="HTML",
        )
        return

    # Время есть. Нет даты → сегодня; если сегодня уже прошло — переносим на завтра.
    rolled = False
    d = the_date or datetime.now(TZ).date()
    if the_date is None:
        candidate = datetime(d.year, d.month, d.day, the_time[0], the_time[1])
        if candidate <= datetime.now(TZ).replace(tzinfo=None):
            d = d + timedelta(days=1)
            rolled = True
    await _create_reminder(update, context, task, d, the_time, "none", rolled=rolled)


# ───────────────────────── Обработчики команд ──────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return

    await update.message.reply_text(
        "Привет! Я твой личный планер. ⏱ Время — по Бали (UTC+8).\n\n"
        "Просто напиши или скажи 🎤 задачу — я сам пойму когда:\n"
        "<i>завтра в 15:00 встреча с клиентом</i>\n"
        "<i>позвонить маме через 2 часа</i>\n"
        "<i>15.06 в 10:00 оплатить аренду</i>\n"
        "<i>ДР мамы 20 августа</i>\n\n"
        "Если скажешь только дату — переспрошу время.\n"
        "Удалить: скажи «<i>удали зал</i>» или нажми 🗑 под задачей.\n\n"
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


def task_line(dt: datetime, text: str) -> str:
    """Строка задачи в стиле v1.1: моноширинное время + эмодзи + текст."""
    return f"<code>{dt:%H:%M}</code> {pick_emoji(text)} {text}"


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
    lines = ["📋 <b>Задачи на сегодня:</b>\n"]
    items = []
    for r in rows:
        dt = datetime.fromisoformat(r["remind_at"])
        lines.append(task_line(dt, r["text"]))
        items.append(("today", r["id"], f"{dt:%H:%M} {r['text']}"))
    return "\n".join(lines), _delete_keyboard(items)


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
    lines = []
    items = []
    current_day = None
    for r in rows:
        dt = datetime.fromisoformat(r["remind_at"])
        d = dt.date()
        if d != current_day:               # новый день → заголовок с датой
            if lines:
                lines.append("")           # пустая строка между днями
            lines.append(f"📅 <b>{ru_date_header(d)}</b>")
            current_day = d
        lines.append(task_line(dt, r["text"]))
        items.append(("week", r["id"], f"{dt:%d.%m %H:%M} {r['text']}"))
    return "\n".join(lines), _delete_keyboard(items)


def render_list():
    reminders = [r for r in get_active_reminders() if r["recurrence"] == "none"]
    if not reminders:
        return "✨ Задач нет. Просто напиши, что и когда напомнить.", None

    lines = ["📋 <b>Твои задачи:</b>\n"]
    items = []
    for r in reminders:
        dt = datetime.fromisoformat(r["remind_at"])
        lines.append(f"{pick_emoji(r['text'])} {r['text']} <code>{format_when(dt)}</code>")
        items.append(("list", r["id"], f"{dt:%d.%m %H:%M} {r['text']}"))
    return "\n".join(lines), _delete_keyboard(items)


def render_birthdays():
    bdays = [r for r in get_active_reminders() if r["recurrence"] == "yearly"]
    if not bdays:
        return "🎂 Дней рождения пока нет.\nДобавь, например: ДР мамы 20 августа", None

    today = datetime.now(TZ).date()
    bdays.sort(key=lambda r: _next_birthday(r, today))  # ближайший — сверху

    lines = ["🎂 <b>Дни рождения:</b>\n"]
    items = []
    for i, r in enumerate(bdays):
        d = datetime.fromisoformat(r["remind_at"]).date()
        suffix = ""
        if i == 0:  # «через N дней» считаем только до ближайшего
            days = (_next_birthday(r, today) - today).days
            if days == 0:
                suffix = " (сегодня! 🎉)"
            elif days == 1:
                suffix = " (завтра)"
            else:
                suffix = f" (через {days} {days_word(days)})"
        lines.append(f"<code>{ru_day_month(d)}</code> — {r['text']}{suffix}")
        items.append(("bday", r["id"], f"{ru_day_month(d)} {r['text']}"))
    return "\n".join(lines), _delete_keyboard(items)


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
    """Принимает обычный текст (не команду) и обрабатывает как задачу."""
    if not is_owner(update):
        return
    await process_free_text(update, context, update.message.text)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Принимает голосовое сообщение:
    скачивает .ogg → распознаёт через stt.transcribe (Groq/Whisper) → обрабатывает как текст.
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
