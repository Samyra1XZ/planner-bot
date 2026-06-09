from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from database import DEFAULT_TZ, get_user_timezone

# Таймзона по умолчанию (Бали, UTC+8) — фолбэк, если у пользователя своя не задана.
# Теперь время считаем в ЛИЧНОЙ таймзоне пользователя, но дефолт держим здесь.
TZ = ZoneInfo(DEFAULT_TZ)

# Планировщик сам по себе должен в какой-то зоне жить; берём дефолтную.
# Конкретные задачи ставим с явной таймзоной их владельца — она и важна.
scheduler = AsyncIOScheduler(timezone=TZ)

# Дни рождения: напоминаем за 2 дня (подумать о подарке) и в сам день, в 09:00.
BIRTHDAY_HOUR = 9


def _as_tz(tz):
    """Принимает строку IANA или ZoneInfo и возвращает ZoneInfo (дефолт — TZ)."""
    if tz is None:
        return TZ
    if isinstance(tz, str):
        try:
            return ZoneInfo(tz)
        except Exception:
            return TZ
    return tz


async def _send_reminder(bot, chat_id: int, text: str):
    # Планировщик вызывает эту функцию автоматически — text уже содержит нужный префикс.
    await bot.send_message(chat_id=chat_id, text=text)


def _schedule_yearly(bot, chat_id: int, reminder_id: int, text: str, dt: datetime, tz) -> bool:
    """
    Ставит ежегодные напоминания о дне рождения через cron (год игнорируется,
    срабатывает каждый год). Два напоминания: за 2 дня и в сам день — в таймзоне tz.
    """
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
                hour=BIRTHDAY_HOUR, minute=0, timezone=tz,
            ),
            args=[bot, chat_id, message],
            id=f"{reminder_id}_bd{lead}",
            replace_existing=True,
        )
    return True


def schedule_reminder(bot, chat_id: int, reminder_id: int, text: str,
                      remind_at: str, recurrence: str = "none", tz=None) -> bool:
    """
    Ставит напоминание в ЛИЧНОЙ таймзоне пользователя (tz — строка IANA или ZoneInfo).
    remind_at — ISO 'YYYY-MM-DD HH:MM' в местном времени пользователя.
    recurrence='yearly' → ежегодный день рождения; иначе разовое на дату.
    Возвращает True если что-то поставлено, False если разовое время уже прошло.
    """
    tz = _as_tz(tz)
    # Строку из БД считаем местным временем пользователя — явно вешаем его таймзону.
    dt = datetime.fromisoformat(remind_at).replace(tzinfo=tz)

    if recurrence == "yearly":
        return _schedule_yearly(bot, chat_id, reminder_id, text, dt, tz)

    now = datetime.now(tz)
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
    и ежегодные (_bd2 + _bd0). id напоминания глобально уникален, так что
    префикс пользователя не нужен. Несуществующие тихо игнорируем.
    """
    for job_id in (
        str(reminder_id), f"{reminder_id}_pre",
        f"{reminder_id}_bd2", f"{reminder_id}_bd0",
    ):
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass  # такого джоба нет — это нормально


def reschedule_all(bot, reminders):
    """
    При перезапуске бота восстанавливает напоминания ВСЕХ пользователей из базы,
    каждое — в таймзоне его владельца. reminders — строки с полем user_id.
    """
    for reminder in reminders:
        user_id = reminder["user_id"]
        schedule_reminder(
            bot,
            user_id,                       # личке пользователя chat_id == user_id
            reminder["id"],
            reminder["text"],
            reminder["remind_at"],
            reminder["recurrence"],
            tz=get_user_timezone(user_id),
        )
