# database.py
import sqlite3
from datetime import datetime

DB_NAME = "bot.db"


def get_connection():
    """
    Каждый раз открываем новое соединение.
    Для aiogram так проще и безопаснее.
    """
    return sqlite3.connect(DB_NAME)


def init_db():
    conn = get_connection()
    cur = conn.cursor()

    # Пользователи
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id           INTEGER UNIQUE,
        username        TEXT,
        first_name      TEXT,
        last_name       TEXT,
        referrer_id     INTEGER,           -- кто пригласил (1 уровень)
        balance_usdt    REAL DEFAULT 0,    -- баланс за рефералку
        reg_date        TEXT,
        is_admin        INTEGER DEFAULT 0,
        FOREIGN KEY(referrer_id) REFERENCES users(id)
    );
    """)

    # Подписки (сигналы/доступ)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS subscriptions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         INTEGER,
        tariff_code     TEXT,              -- 'LIFE100', 'SIG1M50', 'SIG2M80' и т.п.
        start_date      TEXT,
        end_date        TEXT,
        is_lifetime     INTEGER DEFAULT 0,
        active          INTEGER DEFAULT 1,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    """)

    # Платежи
    cur.execute("""
    CREATE TABLE IF NOT EXISTS payments (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         INTEGER,
        tariff_code     TEXT,
        amount_usdt     REAL,
        tx_id           TEXT,              -- хеш/ID транзакции или номер квитанции
        status          TEXT,              -- 'pending', 'paid', 'rejected'
        created_at      TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    """)

    # Начисления по реферальной программе
    cur.execute("""
    CREATE TABLE IF NOT EXISTS referral_rewards (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         INTEGER,           -- кому начислили (реферал)
        from_user_id    INTEGER,           -- кто оплатил
        level           INTEGER,           -- 1 или 2
        amount_usdt     REAL,
        payment_id      INTEGER,
        created_at      TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id),
        FOREIGN KEY(from_user_id) REFERENCES users(id),
        FOREIGN KEY(payment_id) REFERENCES payments(id)
    );
    """)

    # Заявки на вывод
    cur.execute("""
    CREATE TABLE IF NOT EXISTS payout_requests (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         INTEGER,
        amount_usdt     REAL,
        wallet          TEXT,              -- кошелек/крипта
        status          TEXT,              -- 'new', 'approved', 'rejected', 'paid'
        created_at      TEXT,
        processed_at    TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    """)

    conn.commit()
    conn.close()


# ================== Утилиты ==================

def get_or_create_user(tg_id: int, username: str = None,
                       first_name: str = None, last_name: str = None,
                       referrer_tg_id: int | None = None):
    """
    Создаёт пользователя, если его нет.
    referrer_tg_id — это tg_id пригласившего (из реф-ссылки).
    Возвращает запись пользователя (кортеж).
    """
    conn = get_connection()
    cur = conn.cursor()

    # Проверяем, есть ли уже
    cur.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
    row = cur.fetchone()
    if row:
        conn.close()
        return row

    # Ищем реферера, если был передан
    referrer_id = None
    if referrer_tg_id:
        cur.execute("SELECT id FROM users WHERE tg_id = ?", (referrer_tg_id,))
        ref_row = cur.fetchone()
        if ref_row:
            referrer_id = ref_row[0]

    reg_date = datetime.utcnow().isoformat()

    cur.execute("""
        INSERT INTO users (tg_id, username, first_name, last_name, referrer_id, reg_date)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (tg_id, username, first_name, last_name, referrer_id, reg_date))

    conn.commit()

    cur.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
    row = cur.fetchone()
    conn.close()
    return row


def add_payment(user_id: int, tariff_code: str, amount_usdt: float,
                tx_id: str, status: str = "pending"):
    conn = get_connection()
    cur = conn.cursor()

    created_at = datetime.utcnow().isoformat()

    cur.execute("""
        INSERT INTO payments (user_id, tariff_code, amount_usdt, tx_id, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_id, tariff_code, amount_usdt, tx_id, status, created_at))

    payment_id = cur.lastrowid
    conn.commit()
    conn.close()
    return payment_id


def mark_payment_paid(payment_id: int):
    """
    Помечаем платеж оплаченным.
    Здесь же потом можно вешать логику:
    - создать подписку
    - начислить реферальные
    """
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("UPDATE payments SET status = 'paid' WHERE id = ?", (payment_id,))
    conn.commit()
    conn.close()


def create_subscription(user_id: int, tariff_code: str,
                        duration_days: int | None, is_lifetime: bool = False):
    """
    Создаём подписку. Для пожизненного тарифа duration_days = None, is_lifetime = True.
    Для сигналов на 1/2 месяца — передаём количество дней.
    """
    from datetime import timedelta

    conn = get_connection()
    cur = conn.cursor()

    start = datetime.utcnow()
    if is_lifetime or duration_days is None:
        end = None
    else:
        end = start + timedelta(days=duration_days)

    start_str = start.isoformat()
    end_str = end.isoformat() if end else None

    # Деактивируем старые активные подписки, если нужно (например только одна подписка)
    cur.execute("""
        UPDATE subscriptions
        SET active = 0
        WHERE user_id = ? AND active = 1
    """, (user_id,))

    cur.execute("""
        INSERT INTO subscriptions (user_id, tariff_code, start_date, end_date, is_lifetime, active)
        VALUES (?, ?, ?, ?, ?, 1)
    """, (user_id, tariff_code, start_str, end_str, int(is_lifetime)))

    sub_id = cur.lastrowid
    conn.commit()
    conn.close()
    return sub_id


def user_has_active_signals(user_id: int) -> bool:
    """
    Проверяет, есть ли у юзера активная подписка (для сигналов/доступа).
    """
    conn = get_connection()
    cur = conn.cursor()

    now = datetime.utcnow().isoformat()

    cur.execute("""
        SELECT id FROM subscriptions
        WHERE user_id = ?
          AND active = 1
          AND (
            is_lifetime = 1
            OR (end_date IS NOT NULL AND end_date > ?)
          )
        LIMIT 1
    """, (user_id, now))

    row = cur.fetchone()
    conn.close()
    return row is not None
