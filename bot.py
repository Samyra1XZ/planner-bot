import os
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from database import init_db, add_reminder, get_active_reminders, mark_done
from scheduler import scheduler, schedule_reminder, reschedule_all

# Загружаем переменные из файла .env (BOT_TOKEN, MY_CHAT_ID)
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
MY_CHAT_ID = int(os.getenv("MY_CHAT_ID"))


def is_owner(update: Update) -> bool:
    """Проверяет, что команду отправил именно я, а не кто-то чужой."""
    return update.effective_user.id == MY_CHAT_ID


# ───────────────────────── Обработчики команд ──────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start — приветствие и список команд."""
    if not is_owner(update):
        return

    await update.message.reply_text(
        "Привет! Я твой личный планер.\n\n"
        "Команды:\n"
        "/add 15:00 текст — добавить напоминание на сегодня\n"
        "/list — показать все активные напоминания\n"
        "/done 3 — отметить напоминание #3 выполненным"
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /add 15:00 написать клиенту
    context.args — список слов после команды: ['15:00', 'написать', 'клиенту']
    """
    if not is_owner(update):
        return

    # Проверяем что передали хотя бы время и одно слово текста
    if len(context.args) < 2:
        await update.message.reply_text(
            "Не хватает аргументов.\n"
            "Формат: /add 15:00 текст напоминания"
        )
        return

    time_str = context.args[0]                    # '15:00'
    text = " ".join(context.args[1:])             # 'написать клиенту'

    # Проверяем что время в правильном формате
    try:
        datetime.strptime(time_str, "%H:%M")
    except ValueError:
        await update.message.reply_text(
            "Неверный формат времени. Нужно ЧЧ:ММ, например: /add 09:30 позвонить"
        )
        return

    # Сохраняем в базу и получаем id новой записи
    reminder_id = add_reminder(text, time_str)

    # Ставим задачу в планировщик
    scheduled = schedule_reminder(context.bot, MY_CHAT_ID, reminder_id, text, time_str)

    if scheduled:
        await update.message.reply_text(f"✅ Добавлено: [{time_str}] {text}")
    else:
        # Время уже прошло — в базе сохранено, но сегодня не напомнит
        await update.message.reply_text(
            f"Сохранено, но время {time_str} уже прошло — сегодня напоминания не будет."
        )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/list — показывает все невыполненные напоминания."""
    if not is_owner(update):
        return

    reminders = get_active_reminders()

    if not reminders:
        await update.message.reply_text("Активных напоминаний нет.")
        return

    # Собираем строки вида: #1 [15:00] написать клиенту
    lines = []
    for r in reminders:
        lines.append(f"#{r['id']} [{r['remind_at']}] {r['text']}")

    await update.message.reply_text("\n".join(lines))


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/done 3 — отмечает напоминание #3 выполненным."""
    if not is_owner(update):
        return

    if len(context.args) != 1:
        await update.message.reply_text("Формат: /done <номер>, например /done 3")
        return

    # Проверяем что аргумент — число
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


# ───────────────────────── Запуск ──────────────────────────

async def on_startup(application: Application):
    """
    Вызывается автоматически после инициализации бота, до начала получения сообщений.
    Здесь запускаем планировщик и восстанавливаем напоминания из базы.
    """
    scheduler.start()

    # Загружаем активные напоминания и ставим их обратно в планировщик
    reminders = get_active_reminders()
    reschedule_all(application.bot, MY_CHAT_ID, reminders)
    print(f"Восстановлено напоминаний из базы: {len(reminders)}")


if __name__ == "__main__":
    # Создаём таблицу в БД если её ещё нет
    init_db()

    # Собираем приложение: токен + хук на старт
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
        .build()
    )

    # Регистрируем команды — каждая команда привязана к своей функции
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add",   cmd_add))
    app.add_handler(CommandHandler("list",  cmd_list))
    app.add_handler(CommandHandler("done",  cmd_done))

    print("Бот запущен. Нажми Ctrl+C чтобы остановить.")
    # run_polling() запускает бесконечный цикл: слушаем Telegram, обрабатываем команды
    app.run_polling()
