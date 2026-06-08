import sqlite3
from datetime import datetime

# Имя файла базы данных — будет создан рядом со скриптом
DB_FILE = "reminders.db"


def get_connection():
    """Открывает соединение с базой и возвращает его."""
    conn = sqlite3.connect(DB_FILE)
    # Включаем режим, при котором строки возвращаются как словари (col_name → value)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """
    Создаёт таблицу reminders, если её ещё нет, и мигрирует старую схему.
    remind_at теперь хранит полную дату-время в ISO: 'YYYY-MM-DD HH:MM'.
    recurrence: 'none' (разовое) или 'yearly' (день рождения/годовщина).
    """
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            text       TEXT    NOT NULL,
            remind_at  TEXT    NOT NULL,
            recurrence TEXT    NOT NULL DEFAULT 'none',
            done       INTEGER NOT NULL DEFAULT 0
        )
    """)

    # ── Миграция со старой схемы (без колонки recurrence и с remind_at='HH:MM') ──
    columns = [row["name"] for row in conn.execute("PRAGMA table_info(reminders)")]
    if "recurrence" not in columns:
        conn.execute("ALTER TABLE reminders ADD COLUMN recurrence TEXT NOT NULL DEFAULT 'none'")

    # Старые записи хранили только время ('15:00', длина 5) — дописываем сегодняшнюю дату.
    today = datetime.now().strftime("%Y-%m-%d")
    conn.execute(
        "UPDATE reminders SET remind_at = ? || ' ' || remind_at WHERE length(remind_at) <= 5",
        (today,),
    )

    conn.commit()
    conn.close()


def add_reminder(text: str, remind_at: str, recurrence: str = "none") -> int:
    """
    Добавляет новое напоминание в базу.
    remind_at — строка ISO 'YYYY-MM-DD HH:MM'.
    recurrence — 'none' (разовое) или 'yearly' (ежегодное, для дней рождения).
    Возвращает id только что созданной записи.
    """
    conn = get_connection()
    cursor = conn.execute(
        "INSERT INTO reminders (text, remind_at, recurrence) VALUES (?, ?, ?)",
        (text, remind_at, recurrence)
    )
    conn.commit()
    new_id = cursor.lastrowid  # id созданной записи
    conn.close()
    return new_id


def get_active_reminders():
    """
    Возвращает список всех напоминаний, которые ещё не выполнены (done = 0).
    Каждый элемент — объект Row, к полям можно обращаться как row['text'].
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM reminders WHERE done = 0 ORDER BY remind_at"
    ).fetchall()
    conn.close()
    return rows


def mark_done(reminder_id: int) -> bool:
    """
    Помечает напоминание с указанным id как выполненное (done = 1).
    Возвращает True, если запись нашлась и обновилась, False — если нет.
    """
    conn = get_connection()
    cursor = conn.execute(
        "UPDATE reminders SET done = 1 WHERE id = ? AND done = 0",
        (reminder_id,)
    )
    conn.commit()
    updated = cursor.rowcount > 0  # rowcount — сколько строк изменилось
    conn.close()
    return updated


def get_reminder_by_id(reminder_id: int):
    """Возвращает одно напоминание по id (или None, если не нашли)."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM reminders WHERE id = ?",
        (reminder_id,)
    ).fetchone()
    conn.close()
    return row
