import os
import sqlite3
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

# Путь к базе. По умолчанию файл рядом со скриптом.
# На Railway укажи DB_PATH=/data/reminders.db и примонтируй Volume в /data —
# иначе база (а с ней ВСЕ будущие задачи и дни рождения) сбрасывается при каждом редеплое.
DB_FILE = os.getenv("DB_PATH", "reminders.db")

# Таймзона по умолчанию, пока у пользователя не выбрана своя (онбординга ещё нет).
DEFAULT_TZ = "Asia/Makassar"


def get_connection():
    """Открывает соединение с базой и возвращает его."""
    conn = sqlite3.connect(DB_FILE)
    # Включаем режим, при котором строки возвращаются как словари (col_name → value)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(owner_id: int = None):
    """
    Создаёт таблицы, если их ещё нет, и мигрирует старую схему.

    reminders: задачи и дни рождения. Теперь у каждой строки есть user_id —
               чьё это напоминание (продуктовая многопользовательская схема).
    users:     по строке на пользователя со своей таймзоной и языком.

    owner_id (если передан) используется для миграции: все старые задачи без
    user_id привязываются к владельцу, и владельцу заводится строка в users.
    """
    # Если DB_PATH в подкаталоге (например /data/reminders.db) — создаём его.
    parent = os.path.dirname(DB_FILE)
    if parent:
        os.makedirs(parent, exist_ok=True)

    conn = get_connection()

    # ── Таблица задач/напоминаний ──
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            text       TEXT    NOT NULL,
            remind_at  TEXT    NOT NULL,
            recurrence TEXT    NOT NULL DEFAULT 'none',
            done       INTEGER NOT NULL DEFAULT 0
        )
    """)

    # ── Таблица пользователей (для мультиюзера и личной таймзоны) ──
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            timezone    TEXT NOT NULL DEFAULT 'Asia/Makassar',
            language    TEXT NOT NULL DEFAULT 'ru',
            digest_time TEXT NOT NULL DEFAULT '08:00',
            digest_on   INTEGER NOT NULL DEFAULT 1,
            onboarded   INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # Белый список доступа: кому разрешено пользоваться ботом (владелец всегда).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS access (
            user_id  INTEGER PRIMARY KEY,
            added_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # Выполненные «на сегодня» экземпляры повторяющихся задач (повтор не закрывается весь).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS done_instances (
            reminder_id INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            day         TEXT    NOT NULL,
            PRIMARY KEY (reminder_id, day)
        )
    """)

    # Миграция users: добавляем настройки дайджеста и флаг онбординга, если их ещё нет.
    ucolumns = [row["name"] for row in conn.execute("PRAGMA table_info(users)")]
    if "digest_time" not in ucolumns:
        conn.execute("ALTER TABLE users ADD COLUMN digest_time TEXT NOT NULL DEFAULT '08:00'")
    if "digest_on" not in ucolumns:
        conn.execute("ALTER TABLE users ADD COLUMN digest_on INTEGER NOT NULL DEFAULT 1")
    if "onboarded" not in ucolumns:
        conn.execute("ALTER TABLE users ADD COLUMN onboarded INTEGER NOT NULL DEFAULT 0")

    # ── Миграции со старых схем ──
    columns = [row["name"] for row in conn.execute("PRAGMA table_info(reminders)")]

    # Старая схема без recurrence (разовое/ежегодное).
    if "recurrence" not in columns:
        conn.execute("ALTER TABLE reminders ADD COLUMN recurrence TEXT NOT NULL DEFAULT 'none'")

    # Старая схема без user_id — добавляем колонку и привязываем задачи к владельцу.
    if "user_id" not in columns:
        conn.execute("ALTER TABLE reminders ADD COLUMN user_id INTEGER")

    # Колонка времени выполнения (для счётчика «закрыто за сегодня»).
    if "done_at" not in columns:
        conn.execute("ALTER TABLE reminders ADD COLUMN done_at TEXT")

    # Задачи без времени (чек-лист): flexible=1 и порядковый номер ord.
    if "flexible" not in columns:
        conn.execute("ALTER TABLE reminders ADD COLUMN flexible INTEGER NOT NULL DEFAULT 0")
    if "ord" not in columns:
        conn.execute("ALTER TABLE reminders ADD COLUMN ord INTEGER NOT NULL DEFAULT 0")

    # Любые осиротевшие задачи (user_id IS NULL) отдаём владельцу, чтобы не потерять.
    if owner_id is not None:
        conn.execute("UPDATE reminders SET user_id = ? WHERE user_id IS NULL", (owner_id,))

    # Старые записи хранили только время ('15:00', длина 5) — дописываем сегодняшнюю дату.
    today = datetime.now().strftime("%Y-%m-%d")
    conn.execute(
        "UPDATE reminders SET remind_at = ? || ' ' || remind_at WHERE length(remind_at) <= 5",
        (today,),
    )

    conn.commit()
    conn.close()

    # Заводим владельцу строку в users и помечаем онбординг пройденным
    # (его таймзона уже Asia/Makassar — переспрашивать не нужно).
    if owner_id is not None:
        ensure_user(owner_id)
        mark_onboarded(owner_id)


# ───────────────────────── Пользователи ──────────────────────────

def ensure_user(user_id: int):
    """Создаёт пользователя с настройками по умолчанию, если его ещё нет."""
    conn = get_connection()
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, timezone) VALUES (?, ?)",
        (user_id, DEFAULT_TZ),
    )
    conn.commit()
    conn.close()


def get_user(user_id: int):
    """Возвращает строку пользователя (или None, если не зарегистрирован)."""
    conn = get_connection()
    row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row


def get_user_timezone(user_id: int) -> str:
    """Таймзона пользователя (строка IANA). Если пользователя нет — DEFAULT_TZ."""
    row = get_user(user_id)
    return row["timezone"] if row else DEFAULT_TZ


def set_user_timezone(user_id: int, timezone: str):
    """Ставит/меняет таймзону пользователя (создаёт его при необходимости)."""
    ensure_user(user_id)
    conn = get_connection()
    conn.execute("UPDATE users SET timezone = ? WHERE user_id = ?", (timezone, user_id))
    conn.commit()
    conn.close()


def get_all_users():
    """Все пользователи — для планирования дайджестов при старте бота."""
    conn = get_connection()
    rows = conn.execute("SELECT * FROM users").fetchall()
    conn.close()
    return rows


def mark_onboarded(user_id: int):
    """Помечает, что пользователь прошёл онбординг (выбрал таймзону)."""
    ensure_user(user_id)
    conn = get_connection()
    conn.execute("UPDATE users SET onboarded = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


# ───────────────────────── Доступ (белый список) ──────────────────────────

def is_user_allowed(user_id: int) -> bool:
    """Есть ли пользователь в белом списке доступа."""
    conn = get_connection()
    row = conn.execute("SELECT 1 FROM access WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row is not None


def allow_user(user_id: int):
    """Выдаёт доступ пользователю (добавляет в белый список)."""
    conn = get_connection()
    conn.execute("INSERT OR IGNORE INTO access (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()


def deny_user(user_id: int):
    """Забирает доступ у пользователя."""
    conn = get_connection()
    conn.execute("DELETE FROM access WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def set_user_digest(user_id: int, digest_time: str = None, digest_on: int = None):
    """Меняет настройки утреннего дайджеста (время и/или вкл-выкл). Создаёт юзера."""
    ensure_user(user_id)
    conn = get_connection()
    if digest_time is not None:
        conn.execute("UPDATE users SET digest_time = ? WHERE user_id = ?", (digest_time, user_id))
    if digest_on is not None:
        conn.execute("UPDATE users SET digest_on = ? WHERE user_id = ?", (digest_on, user_id))
    conn.commit()
    conn.close()


# ───────────────────────── Задачи (всё в рамках user_id) ──────────────────────────

def add_reminder(user_id: int, text: str, remind_at: str, recurrence: str = "none",
                 flexible: int = 0, ord: int = 0) -> int:
    """
    Добавляет напоминание/задачу пользователю.
    remind_at — 'YYYY-MM-DD HH:MM' (с временем) либо 'YYYY-MM-DD' (без времени, flexible).
    recurrence — 'none' (разовое) или 'yearly' (ежегодное).
    flexible — 1 для задачи без времени (чек-лист), ord — её позиция в списке.
    Возвращает id созданной записи.
    """
    conn = get_connection()
    cursor = conn.execute(
        "INSERT INTO reminders (user_id, text, remind_at, recurrence, flexible, ord) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, text, remind_at, recurrence, flexible, ord),
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()
    return new_id


def update_reminder_ord(user_id: int, reminder_id: int, ord: int):
    """Меняет позицию (ord) задачи без времени — для перестановки чек-листа."""
    conn = get_connection()
    conn.execute(
        "UPDATE reminders SET ord = ? WHERE id = ? AND user_id = ?",
        (ord, reminder_id, user_id),
    )
    conn.commit()
    conn.close()


def next_flexible_ord(user_id: int, day_iso: str) -> int:
    """Следующий порядковый номер для задачи без времени на указанный день."""
    conn = get_connection()
    row = conn.execute(
        "SELECT COALESCE(MAX(ord), 0) + 1 AS n FROM reminders "
        "WHERE user_id = ? AND flexible = 1 AND done = 0 AND remind_at = ?",
        (user_id, day_iso),
    ).fetchone()
    conn.close()
    return row["n"]


def get_active_reminders(user_id: int):
    """Все невыполненные напоминания пользователя (done = 0), по времени."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM reminders WHERE done = 0 AND user_id = ? ORDER BY remind_at",
        (user_id,),
    ).fetchall()
    conn.close()
    return rows


def get_all_active_reminders():
    """
    Все невыполненные напоминания ВСЕХ пользователей (для восстановления
    расписания при старте бота). В строках есть user_id — кому слать.
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM reminders WHERE done = 0 ORDER BY remind_at"
    ).fetchall()
    conn.close()
    return rows


def mark_done(user_id: int, reminder_id: int, done_at: str = None) -> bool:
    """
    Помечает напоминание пользователя выполненным и запоминает время (done_at,
    строка 'YYYY-MM-DD HH:MM' в местном времени). True, если нашлось и обновилось.
    """
    conn = get_connection()
    cursor = conn.execute(
        "UPDATE reminders SET done = 1, done_at = ? WHERE id = ? AND user_id = ? AND done = 0",
        (done_at, reminder_id, user_id),
    )
    conn.commit()
    updated = cursor.rowcount > 0
    conn.close()
    return updated


def count_done_on(user_id: int, day_iso: str) -> int:
    """Сколько задач пользователь закрыл в указанный день (day_iso = 'YYYY-MM-DD')."""
    conn = get_connection()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM reminders WHERE user_id = ? AND done = 1 AND done_at LIKE ?",
        (user_id, day_iso + "%"),
    ).fetchone()
    conn.close()
    return row["c"]


def mark_instance_done(user_id: int, reminder_id: int, day: str):
    """Отмечает повторяющуюся задачу выполненной НА ДЕНЬ day ('YYYY-MM-DD')."""
    conn = get_connection()
    conn.execute(
        "INSERT OR IGNORE INTO done_instances (reminder_id, user_id, day) VALUES (?, ?, ?)",
        (reminder_id, user_id, day),
    )
    conn.commit()
    conn.close()


def is_instance_done(reminder_id: int, day: str) -> bool:
    """Отмечена ли повторяющаяся задача выполненной на указанный день."""
    conn = get_connection()
    row = conn.execute(
        "SELECT 1 FROM done_instances WHERE reminder_id = ? AND day = ?",
        (reminder_id, day),
    ).fetchone()
    conn.close()
    return row is not None


def count_instances_done_on(user_id: int, day: str) -> int:
    """Сколько повторов закрыто «на сегодня» в указанный день."""
    conn = get_connection()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM done_instances WHERE user_id = ? AND day = ?",
        (user_id, day),
    ).fetchone()
    conn.close()
    return row["c"]


def delete_reminder(user_id: int, reminder_id: int) -> bool:
    """Полностью удаляет напоминание пользователя. True, если строка нашлась."""
    conn = get_connection()
    cursor = conn.execute(
        "DELETE FROM reminders WHERE id = ? AND user_id = ?",
        (reminder_id, user_id),
    )
    conn.execute("DELETE FROM done_instances WHERE reminder_id = ?", (reminder_id,))
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    return deleted


def update_reminder_time(user_id: int, reminder_id: int, remind_at: str) -> bool:
    """Меняет время напоминания пользователя (для переноса). True, если обновилось."""
    conn = get_connection()
    cursor = conn.execute(
        "UPDATE reminders SET remind_at = ? WHERE id = ? AND user_id = ?",
        (remind_at, reminder_id, user_id),
    )
    conn.commit()
    updated = cursor.rowcount > 0
    conn.close()
    return updated


def get_reminder_by_id(user_id: int, reminder_id: int):
    """Возвращает напоминание пользователя по id (или None)."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM reminders WHERE id = ? AND user_id = ?",
        (reminder_id, user_id),
    ).fetchone()
    conn.close()
    return row
