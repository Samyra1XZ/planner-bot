from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Один планировщик на всё приложение — создаём здесь, запускаем в bot.py
scheduler = AsyncIOScheduler()


async def _send_reminder(bot, chat_id: int, text: str):
    """
    Эту функцию вызывает планировщик автоматически в нужное время.
    Подчёркивание в начале имени (_send) — соглашение: функция "приватная", только для внутреннего использования.
    """
    await bot.send_message(chat_id=chat_id, text=f"⏰ Напоминание: {text}")


def schedule_reminder(bot, chat_id: int, reminder_id: int, text: str, time_str: str) -> bool:
    """
    Ставит разовое напоминание на сегодня в указанное время.
    time_str — строка вида '15:00'.
    Возвращает True если задача поставлена, False если время уже прошло.
    """
    # Разбиваем '15:00' на часы и минуты
    hour, minute = map(int, time_str.split(":"))

    now = datetime.now()
    # Берём сегодняшнюю дату и подставляем нужные часы и минуты
    run_date = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # Если время уже прошло — не имеет смысла ставить задачу
    if run_date <= now:
        return False

    scheduler.add_job(
        _send_reminder,              # какую функцию вызвать
        trigger="date",              # один раз в конкретный момент (не повторяющийся)
        run_date=run_date,           # когда именно
        args=[bot, chat_id, text],   # аргументы для _send_reminder
        id=str(reminder_id),         # уникальный id задачи (чтобы не дублировать)
        replace_existing=True        # если задача с таким id уже есть — заменить
    )
    return True


def reschedule_all(bot, chat_id: int, reminders):
    """
    При перезапуске бота восстанавливает все активные напоминания из базы.
    Без этого напоминания, добавленные в прошлый раз, потеряются после перезапуска.
    """
    for reminder in reminders:
        schedule_reminder(
            bot,
            chat_id,
            reminder["id"],
            reminder["text"],
            reminder["remind_at"]
        )
