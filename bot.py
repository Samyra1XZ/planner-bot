import asyncio
import os
import tempfile
from datetime import datetime

import dateparser.search
import whisper
from dotenv import load_dotenv
from pydub import AudioSegment
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from database import init_db, add_reminder, get_active_reminders, mark_done
from scheduler import scheduler, schedule_reminder, reschedule_all

# Загружаем переменные из файла .env (BOT_TOKEN, MY_CHAT_ID)
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
MY_CHAT_ID = int(os.getenv("MY_CHAT_ID"))

# Размер модели Whisper для распознавания голоса.
# "base" — лёгкая и быстрая, хватает на слабом железе (Railway/ноут).
# На мощном VPS можно поменять на "small" или "medium" — точность выше.
WHISPER_MODEL = "base"

# Загружаем модель один раз при старте, а не на каждое сообщение —
# загрузка занимает время, поэтому держим её в памяти.
print(f"Загружаю модель Whisper '{WHISPER_MODEL}'... (первый запуск скачает её из интернета)")
whisper_model = whisper.load_model(WHISPER_MODEL)
print("Модель Whisper готова.")


def is_owner(update: Update) -> bool:
    """Проверяет, что команду отправил именно я, а не кто-то чужой."""
    return update.effective_user.id == MY_CHAT_ID


# ───────────────────────── Вспомогательные функции ──────────────────────────

def pick_emoji(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ("тренировка", "спорт", "зал", "бег", "йога", "фитнес")):
        return "🏋"
    if any(w in t for w in ("еда", "завтрак", "обед", "ужин", "кафе", "ресторан", "покушать", "поесть")):
        return "🍽"
    if any(w in t for w in ("встреча", "встретиться", "встретить")):
        return "🤝"
    if any(w in t for w in ("работа", "задача", "чат", "бот", "код", "программ", "разработка", "созвон", "звонок")):
        return "💻"
    return "📌"


def parse_task_from_text(text: str):
    """
    Ищет дату/время в произвольном тексте через dateparser.
    Возвращает (datetime, текст_задачи) или (None, исходный_текст) если время не найдено.
    """
    results = dateparser.search.search_dates(
        text,
        languages=["ru"],
        settings={"PREFER_DATES_FROM": "future", "RETURN_AS_TIMEZONE_AWARE": False},
    )
    if not results:
        return None, text

    # Берём первое найденное совпадение
    matched_str, dt = results[0]
    # Убираем найденную строку с временем из текста задачи
    task_text = text.replace(matched_str, "").strip(" ,.-—")
    if not task_text:
        task_text = text  # если весь текст — это время, оставляем оригинал как задачу
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
            f"✅ Понял! Напомню:\n<code>{time_str}</code>  {emoji} {task_text}",
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
        lines.append(f"<code>{r['remind_at']}</code>  {emoji} {r['text']}")

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
    скачивает .ogg → конвертирует в .wav → распознаёт локально через Whisper → обрабатывает как текст.
    """
    if not is_owner(update):
        return

    await update.message.reply_text("🎤 Слушаю...")

    # Скачиваем голосовой файл во временную директорию
    voice_file = await context.bot.get_file(update.message.voice.file_id)

    with tempfile.TemporaryDirectory() as tmpdir:
        ogg_path = os.path.join(tmpdir, "voice.ogg")
        wav_path = os.path.join(tmpdir, "voice.wav")

        await voice_file.download_to_drive(ogg_path)

        # Конвертируем .ogg (формат Telegram) в .wav — для этого нужен ffmpeg
        AudioSegment.from_ogg(ogg_path).export(wav_path, format="wav")

        # Распознаём речь локально через Whisper.
        # transcribe — тяжёлая блокирующая операция, поэтому уносим её
        # в отдельный поток, чтобы не подвешивать бота на время распознавания.
        try:
            result = await asyncio.to_thread(
                whisper_model.transcribe, wav_path, language="ru"
            )
        except Exception:
            await update.message.reply_text(
                "Не смог разобрать голос. Попробуй ещё раз или напиши текстом."
            )
            return

    text = result["text"].strip()

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
