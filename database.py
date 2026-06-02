import sqlite3

# Имя файла базы данных — будет создан рядом со скриптом
DB_FILE = "reminders.db"


def get_connection():
    """Открывает соединение с базой и возвращает его."""
    conn = sqlite3.connect(DB_FILE)
    # Включаем режим, при котором строки возвращаются как словари (col_name → value)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Создаёт таблицу reminders, если её ещё нет. Вызывается один раз при старте."""
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            text      TEXT    NOT NULL,
            remind_at TEXT    NOT NULL,
            done      INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def add_reminder(text: str, remind_at: str) -> int:
    """
    Добавляет новое напоминание в базу.
    remind_at — строка вида '15:00' (время на сегодня).
    Возвращает id только что созданной записи.
    """
    conn = get_connection()
    cursor = conn.execute(
        "INSERT INTO reminders (text, remind_at) VALUES (?, ?)",
        (text, remind_at)
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
