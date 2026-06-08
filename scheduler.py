from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Один планировщик на всё приложение — создаём здесь, запускаем в bot.py
scheduler = AsyncIOScheduler()


async def _send_reminder(bot, chat_id: int, text: str):
    # Планировщик вызывает эту функцию автоматически — text уже содержит нужный префикс (🔔 или ⏰)
    await bot.send_message(chat_id=chat_id, text=text)


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

    # Основное напоминание — в точное время
    scheduler.add_job(
        _send_reminder,
        trigger="date",
        run_date=run_date,
        args=[bot, chat_id, f"🔔 Сейчас: {text}"],
        id=str(reminder_id),
        replace_existing=True
    )

    # Предварительное напоминание — за 10 минут, если ещё не прошло
    pre_date = run_date - timedelta(minutes=10)
    if pre_date > now:
        scheduler.add_job(
            _send_reminder,
            trigger="date",
            run_date=pre_date,
            args=[bot, chat_id, f"⏰ Через 10 минут: {text}"],
            id=f"{reminder_id}_pre",
            replace_existing=True
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
