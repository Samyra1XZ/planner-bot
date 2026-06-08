from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# Часовой пояс пользователя (Бали, UTC+8). Используем ЯВНО везде:
# и в datetime.now(TZ), и в планировщике — никакого UTC/серверного времени.
TZ = ZoneInfo("Asia/Makassar")

# Один планировщик на всё приложение — создаём здесь, запускаем в bot.py
scheduler = AsyncIOScheduler(timezone=TZ)

# Дни рождения: напоминаем за 2 дня (подумать о подарке) и в сам день, в 09:00.
BIRTHDAY_HOUR = 9


async def _send_reminder(bot, chat_id: int, text: str):
    # Планировщик вызывает эту функцию автоматически — text уже содержит нужный префикс.
    await bot.send_message(chat_id=chat_id, text=text)


def _schedule_yearly(bot, chat_id: int, reminder_id: int, text: str, dt: datetime) -> bool:
    """
    Ставит ежегодные напоминания о дне рождения через cron (год игнорируется,
    срабатывает каждый год). Два напоминания: за 2 дня и в сам день.
    """
    # (за сколько дней, текст напоминания)
    schedule = [
        (2, f"🎂 Через 2 дня ДР у {text} — подумай о подарке"),
        (0, f"🎂 Сегодня день рождения: {text}"),
    ]
    for lead, message in schedule:
        # Считаем дату «за lead дней» в високосном опорном году (чтобы 29 февраля жило).
        target = date(2024, dt.month, dt.day) - timedelta(days=lead)
        scheduler.add_job(
            _send_reminder,
            trigger=CronTrigger(
                month=target.month, day=target.day,
                hour=BIRTHDAY_HOUR, minute=0, timezone=TZ,
            ),
            args=[bot, chat_id, message],
            id=f"{reminder_id}_bd{lead}",
            replace_existing=True,
        )
    return True


def schedule_reminder(bot, chat_id: int, reminder_id: int, text: str,
                      remind_at: str, recurrence: str = "none") -> bool:
    """
    Ставит напоминание.
    remind_at — ISO 'YYYY-MM-DD HH:MM' в местном времени (Asia/Makassar).
    recurrence='yearly' → ежегодный день рождения; иначе разовое на дату.
    Возвращает True если что-то поставлено, False если разовое время уже прошло.
    """
    # Строку из БД считаем местным временем — явно вешаем TZ.
    dt = datetime.fromisoformat(remind_at).replace(tzinfo=TZ)

    if recurrence == "yearly":
        return _schedule_yearly(bot, chat_id, reminder_id, text, dt)

    now = datetime.now(TZ)
    # Разовое время уже прошло — ставить нет смысла
    if dt <= now:
        return False

    # Основное напоминание — в точное время
    scheduler.add_job(
        _send_reminder,
        trigger="date",
        run_date=dt,
        args=[bot, chat_id, f"🔔 Сейчас: {text}"],
        id=str(reminder_id),
        replace_existing=True,
    )

    # Предварительное напоминание — за 10 минут, если ещё не прошло
    pre_date = dt - timedelta(minutes=10)
    if pre_date > now:
        scheduler.add_job(
            _send_reminder,
            trigger="date",
            run_date=pre_date,
            args=[bot, chat_id, f"⏰ Через 10 минут: {text}"],
            id=f"{reminder_id}_pre",
            replace_existing=True,
        )

    return True


def unschedule_reminder(reminder_id: int):
    """
    Снимает все джобы напоминания (на случай удаления): и разовые (id + _pre),
    и ежегодные (_bd2 + _bd0). Несуществующие тихо игнорируем.
    """
    for job_id in (
        str(reminder_id), f"{reminder_id}_pre",
        f"{reminder_id}_bd2", f"{reminder_id}_bd0",
    ):
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass  # такого джоба нет — это нормально


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
            reminder["remind_at"],
            reminder["recurrence"],
        )
