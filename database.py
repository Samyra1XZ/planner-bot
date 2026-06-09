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
            user_id    INTEGER PRIMARY KEY,
            timezone   TEXT NOT NULL DEFAULT 'Asia/Makassar',
            language   TEXT NOT NULL DEFAULT 'ru',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # ── Миграции со старых схем ──
    columns = [row["name"] for row in conn.execute("PRAGMA table_info(reminders)")]

    # Старая схема без recurrence (разовое/ежегодное).
    if "recurrence" not in columns:
        conn.execute("ALTER TABLE reminders ADD COLUMN recurrence TEXT NOT NULL DEFAULT 'none'")

    # Старая схема без user_id — добавляем колонку и привязываем задачи к владельцу.
    if "user_id" not in columns:
        conn.execute("ALTER TABLE reminders ADD COLUMN user_id INTEGER")

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

    # Заводим владельцу строку в users с таймзоной по умолчанию (если ещё нет).
    if owner_id is not None:
        ensure_user(owner_id)


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


# ───────────────────────── Задачи (всё в рамках user_id) ──────────────────────────

def add_reminder(user_id: int, text: str, remind_at: str, recurrence: str = "none") -> int:
    """
    Добавляет напоминание пользователю.
    remind_at — строка ISO 'YYYY-MM-DD HH:MM' (в местном времени пользователя).
    recurrence — 'none' (разовое) или 'yearly' (ежегодное, для дней рождения).
    Возвращает id созданной записи.
    """
    conn = get_connection()
    cursor = conn.execute(
        "INSERT INTO reminders (user_id, text, remind_at, recurrence) VALUES (?, ?, ?, ?)",
        (user_id, text, remind_at, recurrence),
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()
    return new_id


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


def mark_done(user_id: int, reminder_id: int) -> bool:
    """Помечает напоминание пользователя выполненным. True, если нашлось и обновилось."""
    conn = get_connection()
    cursor = conn.execute(
        "UPDATE reminders SET done = 1 WHERE id = ? AND user_id = ? AND done = 0",
        (reminder_id, user_id),
    )
    conn.commit()
    updated = cursor.rowcount > 0
    conn.close()
    return updated


def delete_reminder(user_id: int, reminder_id: int) -> bool:
    """Полностью удаляет напоминание пользователя. True, если строка нашлась."""
    conn = get_connection()
    cursor = conn.execute(
        "DELETE FROM reminders WHERE id = ? AND user_id = ?",
        (reminder_id, user_id),
    )
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
