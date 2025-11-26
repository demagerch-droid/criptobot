import os
import logging
import sqlite3
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from aiogram.contrib.middlewares.logging import LoggingMiddleware

# ---------------------------------------------------------------------------
# НАСТРОЙКИ
# ---------------------------------------------------------------------------

BOT_TOKEN = os.getenv("8330326273:AAEuWSwkqi7ypz1LZL4LXRr2jSMpKjGc36k")
ADMIN_ID = int(os.getenv("682938643", "0"))
SUPPORT_CONTACT = os.getenv("SUPPORT_CONTACT", "@support")

PRICE_USD = 100  # стоимость продукта в долларах
LEVEL1_PERCENT = 0.5   # 50% первому уровню
LEVEL2_PERCENT = 0.1   # 10% второму уровню

DB_PATH = "database.db"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot)
dp.middleware.setup(LoggingMiddleware())

# ---------------------------------------------------------------------------
# БАЗА ДАННЫХ
# ---------------------------------------------------------------------------


def db_connect():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE,
            username TEXT,
            first_name TEXT,
            referrer_id INTEGER,
            balance REAL DEFAULT 0,
            total_earned REAL DEFAULT 0,
            reg_date TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            product_code TEXT,
            amount REAL,
            status TEXT,
            created_at TEXT,
            paid_at TEXT,
            tx_id TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE,
            module_key TEXT,
            lesson_index INTEGER
        )
        """
    )

    conn.commit()
    conn.close()


def get_or_create_user(message: types.Message, referrer_id: int = None):
    user_id = message.from_user.id
    username = message.from_user.username or ""
    first_name = message.from_user.first_name or ""

    conn = db_connect()
    cur = conn.cursor()

    cur.execute("SELECT id, referrer_id FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()

    if row:
        user_db_id, existing_referrer = row
        # если пользователь уже есть, реферера не перезаписываем
        conn.close()
        return user_db_id

    reg_date = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        "INSERT INTO users (user_id, username, first_name, referrer_id, reg_date) VALUES (?, ?, ?, ?, ?)",
        (user_id, username, first_name, referrer_id, reg_date),
    )
    conn.commit()
    user_db_id = cur.lastrowid
    conn.close()
    return user_db_id


def get_user_by_user_id(user_id: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, user_id, username, first_name, referrer_id, balance, total_earned "
        "FROM users WHERE user_id = ?",
        (user_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def create_purchase(user_id: int, product_code: str, amount: float) -> int:
    conn = db_connect()
    cur = conn.cursor()
    created_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        "INSERT INTO purchases (user_id, product_code, amount, status, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, product_code, amount, "pending", created_at),
    )
    conn.commit()
    purchase_id = cur.lastrowid
    conn.close()
    return purchase_id


def get_last_pending_purchase(user_id: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, product_code, amount, status, created_at, tx_id FROM purchases "
        "WHERE user_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1",
        (user_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def mark_purchase_paid(purchase_id: int, tx_id: str = None):
    conn = db_connect()
    cur = conn.cursor()
    paid_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        "UPDATE purchases SET status = 'paid', paid_at = ?, tx_id = ? WHERE id = ?",
        (paid_at, tx_id, purchase_id),
    )
    conn.commit()
    conn.close()


def add_balance(user_db_id: int, amount: float):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET balance = balance + ?, total_earned = total_earned + ? WHERE id = ?",
        (amount, amount, user_db_id),
    )
    conn.commit()
    conn.close()


def get_referrer_chain(user_db_id: int):
    """
    Возвращает (id_1го_уровня, id_2го_уровня) в таблице users
    """
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("SELECT referrer_id FROM users WHERE id = ?", (user_db_id,))
    row = cur.fetchone()
    if not row or row[0] is None:
        conn.close()
        return None, None

    lvl1_id = row[0]

    cur.execute("SELECT referrer_id FROM users WHERE id = ?", (lvl1_id,))
    row2 = cur.fetchone()
    lvl2_id = row2[0] if row2 and row2[0] is not None else None

    conn.close()
    return lvl1_id, lvl2_id


def set_progress(user_id: int, module_key: str, lesson_index: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT id FROM progress WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if row:
        cur.execute(
            "UPDATE progress SET module_key = ?, lesson_index = ? WHERE user_id = ?",
            (module_key, lesson_index, user_id),
        )
    else:
        cur.execute(
            "INSERT INTO progress (user_id, module_key, lesson_index) VALUES (?, ?, ?)",
            (user_id, module_key, lesson_index),
        )
    conn.commit()
    conn.close()


def get_progress(user_id: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT module_key, lesson_index FROM progress WHERE user_id = ?",
        (user_id,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None, 0
    return row[0], row[1]


# ---------------------------------------------------------------------------
# АНТИСПАМ
# ---------------------------------------------------------------------------

user_last_action = {}  # type: dict[int, datetime]
ANTISPAM_SECONDS = 1.2  # минимальный интервал между сообщениями


def is_spam(user_id: int) -> bool:
    now = datetime.utcnow()
    last = user_last_action.get(user_id)
    user_last_action[user_id] = now
    if not last:
        return False
    return (now - last) < timedelta(seconds=ANTISPAM_SECONDS)


# ---------------------------------------------------------------------------
# КОНТЕНТ КУРСА
# ---------------------------------------------------------------------------

# ключ -> (название модуля, [уроки])
COURSE = {
    "mindset": (
        "Модуль 1. Психология трейдинга",
        [
            "💡 <b>Урок 1. Кто такой трейдер и за что он получает деньги</b>\n\n"
            "Трейдер – это не угадайщик курса. Это человек, который системно принимает решения в условиях "
            "неопределённости и управляет риском. Тебе не нужно быть гением – достаточно дисциплины и понятной "
            "рабочей стратегии.",

            "💡 <b>Урок 2. Почему 90% сливают депозит</b>\n\n"
            "Главные причины: азарт, желание «отбиться», торговля без плана и риски «на всё плечо».\n"
            "Наша задача – сделать из тебя хладнокровного исполнителя своей стратегии, а не игрока в казино.",

            "💡 <b>Урок 3. Правило одной сделки</b>\n\n"
            "Представь, что у тебя осталась одна единственная сделка в жизни. Зайдёшь ли ты в неё прямо сейчас? "
            "Если ответ «нет» – значит вход плохой. Это простой фильтр, который спасает от импульсивных действий.",
        ],
    ),
    "risk": (
        "Модуль 2. Риск-менеджмент",
        [
            "📊 <b>Урок 1. Сколько можно рисковать в одной сделке</b>\n\n"
            "Золотое правило – не более 1–2% от депозита в одной сделке. Так даже серия убыточных входов не убьёт "
            "счёт и даст возможность «вытащить» его за счёт следующих сделок.",

            "📊 <b>Урок 2. Как считать объём позиции</b>\n\n"
            "1) Определи размер стоп-лосса в %.\n"
            "2) Реши, сколько % от депозита ты готов потерять.\n"
            "3) Делим риск на размер стопа – получаем объём позиции.\n\n"
            "Пример: депозит 1000$, риск 1% (10$), стоп 5%. 10 / 0.05 = 200$ – твой объём сделки.",

            "📊 <b>Урок 3. Легенда про «разгон депозита»</b>\n\n"
            "Красивые скрины разгона счёта – почти всегда маркетинг. Реальный трейдинг – это серия аккуратных "
            "повторяющихся действий, а не случайный «выстрел».",
        ],
    ),
    "strategy": (
        "Модуль 3. Торговая система",
        [
            "📈 <b>Урок 1. Из чего состоит стратегия</b>\n\n"
            "Любая рабочая система включает:\n"
            "• условия входа\n"
            "• условия выхода\n"
            "• управление риском\n"
            "• понятное время для торговли.\n\n"
        ]
