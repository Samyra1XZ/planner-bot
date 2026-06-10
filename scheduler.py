from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from database import DEFAULT_TZ, get_user_timezone

# Таймзона по умолчанию (Бали, UTC+8) — фолбэк, если у пользователя своя не задана.
# Теперь время считаем в ЛИЧНОЙ таймзоне пользователя, но дефолт держим здесь.
TZ = ZoneInfo(DEFAULT_TZ)

# Планировщик сам по себе должен в какой-то зоне жить; берём дефолтную.
# Конкретные задачи ставим с явной таймзоной их владельца — она и важна.
scheduler = AsyncIOScheduler(timezone=TZ)

# Дни рождения: напоминаем за 2 дня (подумать о подарке) и в сам день, в 09:00.
BIRTHDAY_HOUR = 9

# Типы повторяющихся задач и соответствие дню недели для cron (Пн=0).
RECURRING = ("daily", "weekdays", "weekly")
_CRON_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


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


def _reminder_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    """Кнопки под напоминанием: отметить готовым или отложить на 10/30/60 минут."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Готово", callback_data=f"done:remind:{reminder_id}"),
        InlineKeyboardButton("⏰ +10", callback_data=f"snooze:{reminder_id}:10"),
        InlineKeyboardButton("⏰ +30", callback_data=f"snooze:{reminder_id}:30"),
        InlineKeyboardButton("⏰ +60", callback_data=f"snooze:{reminder_id}:60"),
    ]])


async def _send_reminder(bot, chat_id: int, text: str,
                         reminder_id: int = None, with_actions: bool = False):
    # Планировщик вызывает эту функцию автоматически — text уже содержит нужный префикс.
    # with_actions=True — прикрепляем кнопки «Готово/Отложить» (для основного напоминания).
    markup = _reminder_keyboard(reminder_id) if (with_actions and reminder_id is not None) else None
    await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)


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


def _schedule_recurring(bot, chat_id: int, reminder_id: int, text: str,
                        dt: datetime, recurrence: str, tz) -> bool:
    """
    Ставит повторяющееся напоминание через cron в таймзоне tz:
    daily — каждый день; weekdays — Пн-Пт; weekly — в день недели из dt.
    """
    kwargs = dict(hour=dt.hour, minute=dt.minute, timezone=tz)
    if recurrence == "weekdays":
        kwargs["day_of_week"] = "mon-fri"
    elif recurrence == "weekly":
        kwargs["day_of_week"] = _CRON_WEEKDAYS[dt.weekday()]
    # daily — без day_of_week (каждый день)

    scheduler.add_job(
        _send_reminder,
        trigger=CronTrigger(**kwargs),
        args=[bot, chat_id, f"🔁 Сейчас: {text}"],
        id=str(reminder_id),
        replace_existing=True,
    )
    return True


def schedule_reminder(bot, chat_id: int, reminder_id: int, text: str,
                      remind_at: str, recurrence: str = "none", tz=None) -> bool:
    """
    Ставит напоминание в ЛИЧНОЙ таймзоне пользователя (tz — строка IANA или ZoneInfo).
    remind_at — ISO 'YYYY-MM-DD HH:MM' в местном времени пользователя.
    recurrence='yearly' → ежегодный ДР; 'daily'/'weekdays'/'weekly' → повтор;
    иначе разовое на дату. Возвращает True если поставлено, False если время прошло.
    """
    tz = _as_tz(tz)
    # Строку из БД считаем местным временем пользователя — явно вешаем его таймзону.
    dt = datetime.fromisoformat(remind_at).replace(tzinfo=tz)

    if recurrence == "yearly":
        return _schedule_yearly(bot, chat_id, reminder_id, text, dt, tz)

    if recurrence in RECURRING:
        return _schedule_recurring(bot, chat_id, reminder_id, text, dt, recurrence, tz)

    now = datetime.now(tz)
    # Разовое время уже прошло — ставить нет смысла
    if dt <= now:
        return False

    # Основное напоминание — в точное время, с кнопками «Готово/Отложить»
    scheduler.add_job(
        _send_reminder,
        trigger="date",
        run_date=dt,
        args=[bot, chat_id, f"🔔 Сейчас: {text}"],
        kwargs={"reminder_id": reminder_id, "with_actions": True},
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
        # Задачи без времени (чек-лист) не планируем — у них нет часа.
        if reminder["flexible"]:
            continue
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
