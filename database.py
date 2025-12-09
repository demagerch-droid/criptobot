# database.py
import sqlite3
from datetime import datetime

DB_NAME = "bot.db"


def get_connection():
    return sqlite3.connect(DB_NAME)


def init_db():
    conn = get_connection()
    cur = conn.cursor()

    # создаём таблицу users, если её ещё нет
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id           INTEGER UNIQUE,
        referrer_tg_id  INTEGER,
        reg_date        TEXT
    );
    """)

    conn.commit()
    conn.close()


# 🔹 ВАЖНО: сразу при импорте файла создаём таблицу
init_db()


def get_or_create_user(tg_id: int, referrer_tg_id: int | None = None):
    """
    Принимает ТОЛЬКО tg_id (число) и referrer_tg_id (число или None).
    """
    conn = get_connection()
    cur = conn.cursor()

    # пробуем найти пользователя
    cur.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
    row = cur.fetchone()
    if row:
        conn.close()
        return row

    # если нет — создаём
    reg_date = datetime.utcnow().isoformat()

    cur.execute(
        "INSERT INTO users (tg_id, referrer_tg_id, reg_date) VALUES (?, ?, ?)",
        (tg_id, referrer_tg_id, reg_date),
    )

    conn.commit()

    cur.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
    row = cur.fetchone()
    conn.close()
    return row
