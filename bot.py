import asyncio
import os
import re
import tempfile
from datetime import datetime, timedelta

import dateparser.search
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from database import init_db, add_reminder, get_active_reminders, mark_done
from scheduler import scheduler, schedule_reminder, reschedule_all
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

# Слова-даты: для нашего «однодневного» бота в названии задачи они не нужны.
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
        base = datetime.now() + timedelta(hours=int(m.group(1)))
        return base.hour, base.minute, m.span()
    m = re.search(r"через\s+(\d{1,3})\s*мин\w*", t)
    if m:
        base = datetime.now() + timedelta(minutes=int(m.group(1)))
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


def parse_task_from_text(text: str):
    """
    Достаёт время и текст задачи из произвольной фразы.
    Возвращает (datetime, текст_задачи) или (None, исходный_текст) если время не найдено.
    Сначала пробуем свой надёжный экстрактор, затем dateparser как запасной вариант.
    """
    found = extract_time(text)
    if found is not None:
        hour, minute, (start, end) = found
        dt = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
        task_text = clean_task_text(text[:start] + " " + text[end:])
        if not task_text:
            task_text = clean_task_text(text)
        return dt, task_text

    # Запасной путь: вдруг dateparser поймёт что-то нестандартное.
    results = dateparser.search.search_dates(
        text,
        languages=["ru"],
        settings={"PREFER_DATES_FROM": "future", "RETURN_AS_TIMEZONE_AWARE": False},
    )
    if not results:
        return None, text

    dt = results[0][1]
    task_text = text
    for matched_str, _ in results:
        task_text = task_text.replace(matched_str, " ")
    task_text = clean_task_text(task_text)
    if not task_text:
        task_text = clean_task_text(text)
    return dt, task_text


async def process_free_text(update: Update, context, raw_text: str):
    """
    Общая логика для текстовых и голосовых сообщений:
    распарсить время → сохранить в базу → запланировать напоминание.
    """
    dt, task_text = parse_task_from_text(raw_text)

    if dt is None:
        await update.message.reply_text(
            "⏰ Не понял время. Напиши когда, например:\n"
            "<i>завтра в 15:00 встреча с клиентом</i>",
            parse_mode="HTML",
        )
        return

    time_str = dt.strftime("%H:%M")
    reminder_id = add_reminder(task_text, time_str)
    scheduled = schedule_reminder(context.bot, MY_CHAT_ID, reminder_id, task_text, time_str)

    emoji = pick_emoji(task_text)
    if scheduled:
        await update.message.reply_text(
            f"✅ Понял! Напомню:\n{emoji} {task_text} <code>в {time_str}</code>",
            parse_mode="HTML",
        )
    else:
        # Время уже прошло — задача сохранена, но сегодня не сработает
        await update.message.reply_text(
            f"Сохранено, но {time_str} уже прошло — сегодня напоминания не будет."
        )


# ───────────────────────── Обработчики команд ──────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return

    await update.message.reply_text(
        "Привет! Я твой личный планер.\n\n"
        "Просто напиши мне задачу с временем:\n"
        "<i>встреча с клиентом в 15:00</i>\n"
        "<i>позвонить маме через 2 часа</i>\n\n"
        "Или отправь голосовое сообщение 🎤\n\n"
        "Команды:\n"
        "/add 15:00 текст — добавить напоминание\n"
        "/list — показать расписание\n"
        "/done 3 — отметить задачу #3 выполненной",
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

    reminder_id = add_reminder(text, time_str)
    scheduled = schedule_reminder(context.bot, MY_CHAT_ID, reminder_id, text, time_str)

    if scheduled:
        await update.message.reply_text(f"✅ Добавлено: [{time_str}] {text}")
    else:
        await update.message.reply_text(
            f"Сохранено, но время {time_str} уже прошло — сегодня напоминания не будет."
        )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return

    reminders = get_active_reminders()

    if not reminders:
        await update.message.reply_text("✨ На сегодня пусто. Добавь задачу через /add")
        return

    lines = ["📋 <b>Твоё расписание на сегодня:</b>\n"]
    for r in reminders:
        emoji = pick_emoji(r["text"])
        lines.append(f"{emoji} {r['text']} <code>в {r['remind_at']}</code>")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


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
    app.add_handler(CommandHandler("list",  cmd_list))
    app.add_handler(CommandHandler("done",  cmd_done))

    # Свободный текст и голос — после команд, чтобы не перехватывать /команды
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    print("Бот запущен. Нажми Ctrl+C чтобы остановить.")
    app.run_polling()
