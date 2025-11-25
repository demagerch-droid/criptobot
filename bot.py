import logging
import sqlite3
from datetime import datetime
import os
import random
import time
from typing import List, Tuple

import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.dispatcher.filters import Text
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

# ==========================
# НАСТРОЙКИ
# ==========================

BOT_TOKEN = os.getenv("BOT_TOKEN", "8330326273:AAEuWSwkqi7ypz1LZL4LXRr2jSMpKjGc36k")
ADMIN_ID = int(os.getenv("ADMIN_ID", "682938643"))

TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "b33b8d65-10c9-4f7b-99e0-ab47f3bbb60f")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "TSY9xf24bQ3Kbd1Njp2w4pEEoqJow1nfpr")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1003464806734"))

PRODUCT_PRICE_USD = 100
REF_L1_PERCENT = 50
REF_L2_PERCENT = 10

SUPPORT_CONTACT = os.getenv("SUPPORT_CONTACT", "@your_support_username")

DB_PATH = "database.db"

# интервалы фоновой проверки
PAYMENT_SCAN_INTERVAL = 60  # 60 секунд
TRON_TRANSACTIONS_LIMIT = 50  # сколько последних транзакций смотреть

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN, parse_mode="Markdown")
dp = Dispatcher(bot)

# ==========================
# БАЗА ДАННЫХ
# ==========================

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        full_name TEXT,
        first_seen TEXT,
        last_active TEXT,
        referrer_id INTEGER,
        balance REAL DEFAULT 0,
        level1_earned REAL DEFAULT 0,
        level2_earned REAL DEFAULT 0,
        total_withdrawn REAL DEFAULT 0,
        has_access INTEGER DEFAULT 0
    );
    """
)

cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS purchases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        base_amount REAL,
        unique_amount REAL,
        status TEXT,
        created_at TEXT,
        confirmed_at TEXT,
        tx_amount REAL,
        tx_time TEXT,
        tx_id TEXT
    );
    """
)

cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS referral_earnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer_id INTEGER,
        referred_id INTEGER,
        level INTEGER,
        bonus REAL,
        created_at TEXT
    );
    """
)

conn.commit()

# ==========================
# АНТИ-СПАМ (простая защита)
# ==========================

user_messages = {}  # user_id -> [timestamps]
SPAM_WINDOW = 10     # секунд
SPAM_LIMIT = 8       # сообщений за окно
SPAM_COOLDOWN = 5    # секунд блокировки

user_spam_block = {}  # user_id -> until_timestamp


async def anti_spam(message: types.Message) -> bool:
    """Простая защита от спама, чтобы не забивали бота."""
    uid = message.from_user.id
    now = time.time()

    # если пользователь заблокирован на время
    until = user_spam_block.get(uid)
    if until and now < until:
        # молча игнорируем
        return False

    times = user_messages.get(uid, [])
    # очищаем старые
    times = [t for t in times if now - t <= SPAM_WINDOW]
    times.append(now)
    user_messages[uid] = times

    if len(times) > SPAM_LIMIT:
        user_spam_block[uid] = now + SPAM_COOLDOWN
        try:
            await message.answer("⏳ Слишком много сообщений подряд. Подожди пару секунд.")
        except Exception:
            pass
        return False

    return True

# ==========================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ БД
# ==========================


def save_user(user: types.User, referrer_id: int = None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    cursor.execute(
        """
        INSERT INTO users (user_id, username, full_name, first_seen, last_active, referrer_id)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            full_name = excluded.full_name,
            last_active = excluded.last_active
        """,
        (
            user.id,
            user.username or "",
            f"{user.first_name or ''} {user.last_name or ''}".strip(),
            now,
            now,
            referrer_id,
        ),
    )
    conn.commit()


def get_user(user_id: int):
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    return cursor.fetchone()


def create_purchase(user_id: int, base_amount: float, unique_amount: float):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    cursor.execute(
        """
        INSERT INTO purchases (user_id, base_amount, unique_amount, status, created_at, confirmed_at, tx_amount, tx_time, tx_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, base_amount, unique_amount, "pending", now, "", 0.0, "", ""),
    )
    conn.commit()


def get_last_pending_purchase(user_id: int):
    cursor.execute(
        """
        SELECT id, user_id, base_amount, unique_amount, status, created_at, confirmed_at, tx_amount, tx_time, tx_id
        FROM purchases
        WHERE user_id = ? AND status = 'pending'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (user_id,),
    )
    return cursor.fetchone()


def get_all_pending_purchases():
    cursor.execute(
        """
        SELECT id, user_id, base_amount, unique_amount, status, created_at, confirmed_at, tx_amount, tx_time, tx_id
        FROM purchases
        WHERE status = 'pending'
        """
    )
    return cursor.fetchall()


def confirm_purchase_record(purchase_id: int, tx_amount: float, tx_time: str, tx_id: str):
    cursor.execute(
        """
        UPDATE purchases
        SET status = 'confirmed',
            confirmed_at = ?,
            tx_amount = ?,
            tx_time = ?,
            tx_id = ?
        WHERE id = ?
        """,
        (datetime.now().strftime("%Y-%m-%d %H:%M"), tx_amount, tx_time, tx_id, purchase_id),
    )
    conn.commit()


def set_access(user_id: int, has_access: bool = True):
    cursor.execute(
        "UPDATE users SET has_access = ? WHERE user_id = ?",
        (1 if has_access else 0, user_id),
    )
    conn.commit()


def add_referral_bonus(referrer_id: int, referred_id: int, level: int, bonus: float):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    cursor.execute(
        """
        INSERT INTO referral_earnings (referrer_id, referred_id, level, bonus, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (referrer_id, referred_id, level, bonus, now),
    )

    if level == 1:
        cursor.execute(
            "UPDATE users SET balance = balance + ?, level1_earned = level1_earned + ? WHERE user_id = ?",
            (bonus, bonus, referrer_id),
        )
    elif level == 2:
        cursor.execute(
            "UPDATE users SET balance = balance + ?, level2_earned = level2_earned + ? WHERE user_id = ?",
            (bonus, bonus, referrer_id),
        )

    conn.commit()

# ==========================
# ОБУЧЕНИЕ: ТРЕЙДИНГ
# ==========================

TRADING_LESSONS: List[Tuple[str, str]] = [
    (
        "Блок 1. Основа трейдинга",
        "🔹 *Что такое трейдинг*\n\n"
        "Трейдинг — это не казино и не угадайка. Это работа с вероятностями, "
        "рисками и понятными правилами.\n\n"
        "В этом блоке ты поймёшь:\n"
        "• чем трейдинг отличается от инвестиций\n"
        "• какие бывают типы ордеров\n"
        "• что такое риск-менеджмент и почему без него ВСЕ сливают\n\n"
        "Главная мысль: *твоя задача — не угадать рынок, а научиться управлять риском*."
    ),
    (
        "Блок 2. Психология и дисциплина",
        "🧠 *Психология трейдинга*\n\n"
        "Большинство сливают не потому что стратегия плохая, а потому что:\n"
        "• увеличивают лот 'на эмоциях'\n"
        "• отыгрываются после убытка\n"
        "• входят в рынок без плана\n\n"
        "Мы делаем упор на:\n"
        "• чёткий торговый план\n"
        "• фиксированный риск на сделку\n"
        "• отсутствие 'угадываний'\n\n"
        "Твоя сила — в дисциплине, а не в гениальности."
    ),
    (
        "Блок 3. Работа с сигналами",
        "📈 *Как работать с сигналами грамотно*\n\n"
        "Сигналы — это подсказка, а не волшебная палочка.\n\n"
        "Твоя задача:\n"
        "• не заходить 'на всё депо'\n"
        "• соблюдать риск 1–3% от депозита на сделку\n"
        "• не открывать 10 сделок одновременно, если депозит маленький\n\n"
        "Сигналы + риск-менеджмент + психология = работающая система."
    ),
    (
        "Блок 4. Путь к стабильности",
        "🚀 *Как прийти к стабильному результату*\n\n"
        "Не жди, что ты станешь миллионером за неделю.\n\n"
        "Реальный путь:\n"
        "• 1–4 недели — базовое понимание, адаптация к стратегии\n"
        "• 1–3 месяца — первые стабильные результаты\n"
        "• 6–12 месяцев — формирование сильного скилла\n\n"
        "Мы даём тебе:\n"
        "• базу и структуру\n"
        "• сигналы\n"
        "• систему заработка на рефералах\n\n"
        "Твоя задача — действовать."
    ),
]

# ==========================
# ОБУЧЕНИЕ ТРАФИКУ
# ==========================

TRAFFIC_LESSONS: List[Tuple[str, str]] = [
    (
        "Урок 1. Суть схемы: TikTok → Telegram → Деньги",
        "TikTok — это бесплатный поток людей.\n\n"
        "Схема проста:\n"
        "1) Ты снимаешь короткие видео с сильными триггерами: деньги, свобода, "
        "изменение жизни.\n"
        "2) В каждом видео ведёшь людей в Telegram-бота.\n"
        "3) В боте человек видит систему: обучение, сигналы, партнёрку 50%/10%.\n"
        f"4) Он покупает доступ за *{PRODUCT_PRICE_USD}$*, и ты забираешь *{PRODUCT_PRICE_USD * REF_L1_PERCENT / 100:.0f}$* как партнёр.\n"
        "5) Если он приводит других — ты забираешь ещё 10% со второго уровня.\n\n"
        "Это не сказка, а воронка: TikTok → бот → продажа → рефералы."
    ),
    (
        "Урок 2. Оформление профиля TikTok",
        "Оформление — это твой первый фильтр.\n\n"
        "Рекомендуется:\n"
        "• Имя: что-то в стиле 'Крипта и доход', 'Путь к $300 в день'.\n"
        "• Аватар: твоя адекватная фотка или логотип проекта.\n"
        "• Описание профиля:\n"
        "  'Обучаю зарабатывать на крипте и партнёрке.\n"
        "   Купил доступ один раз → зарабатываешь постоянно.\n"
        "   Ссылка на систему ниже 👇'\n\n"
        "Главное — сразу дать человеку понять, что ты про ДЕНЬГИ и СИСТЕМУ."
    ),
    (
        "Урок 3. Какие видео заходят лучше всего",
        "Тебе не нужно быть блогером.\n\n"
        "Типы роликов, которые работают:\n"
        "• Боль: 'Работаешь по 10 часов, а денег всё равно нет?'\n"
        "• Возможность: 'Вот схема, как люди делают +50$ за одного человека.'\n"
        "• Схема: 'TikTok → Telegram → заработок 2 источниками.'\n"
        "• Соцдоказательства: скрин дохода, отзыв, история.\n\n"
        "Старайся, чтобы в каждом ролике была эмоция и призыв: 'Ссылка в шапке профиля.'"
    ),
    (
        "Урок 4. Видео без лица",
        "Если не хочешь светиться — это не проблема.\n\n"
        "Форматы контента без лица:\n"
        "• Запись экрана + твой голос.\n"
        "• Текст на фоне + музыка (через CapCut).\n"
        "• Картинки с текстом + закадровый голос.\n\n"
        "Важно не то, как ты выглядишь, а что ты говоришь и насколько это цепляет."
    ),
    (
        "Урок 5. Как правильно вести на ссылку",
        "TikTok не любит прямое слово 'telegram'.\n\n"
        "Делай так:\n"
        "• Ставь ссылку на бота в шапку профиля.\n"
        "• В видео говори: 'Смотри ссылку в профиле' или 'Ссылка в закрепе'.\n"
        "• В комментариях можно закрепить: 'Подробности — в закреплённой ссылке.'\n\n"
        "Не надо писать домены с 't.me' в самом видео — меньше шансов на бан."
    ),
    (
        "Урок 6. План контента на неделю",
        "Стабильность > идеальность.\n\n"
        "Простой план:\n"
        "• Каждый день 1–3 коротких видео.\n"
        "• Чередуй: боль, возможность, история, объяснение схемы.\n"
        "• 30–50 видео — минимальный объём для ощутимого потока людей.\n\n"
        "Главное — не ждать 'идеального ролика', а делать КОЛИЧЕСТВО с нормальным качеством."
    ),
    (
        "Урок 7. Работа с комментариями",
        "Комментарии — это бесплатный прогрев.\n\n"
        "Отвечай так:\n"
        "• 'Реально ли это работает?' — 'Да. У нас 2 источника дохода: трейдинг + реферальная система 50%/10%.'\n"
        "• 'Сколько можно заработать?' — 'Кто-то отбивает 100$ за 2 человек, дальше идёт в плюс.'\n"
        "• 'Это пирамида?' — 'Нет. Ты покупаешь доступ к системе обучения и сигналам. Партнёрка — бонус за то, что делишься.'\n\n"
        "Не спорь и не оправдывайся. Коротко, уверенно, по делу."
    ),
    (
        "Урок 8. Как просто объяснять партнёрку",
        "Говори максимально простыми словами:\n\n"
        f"• 'Ты покупаешь доступ к системе за {PRODUCT_PRICE_USD}$.'\n"
        f"• 'После этого получаешь реферальку: {REF_L1_PERCENT}% с каждого человека, кого приведёшь лично.'\n"
        f"• 'И ещё {REF_L2_PERCENT}% со второго уровня — тех, кого приведут твои люди.'\n\n"
        "Пример:\n"
        "Привёл 3 человек сам → 3 × 50$ = 150$.\n"
        "Они привели ещё людей — ты докручиваешь пассивом по 10$ с каждого второго уровня."
    ),
    (
        "Урок 9. Масштабирование через несколько аккаунтов",
        "Когда почувствуешь себя уверенно — масштабируйся.\n\n"
        "Идеи масштабирования:\n"
        "• Веди 2–3 разных TikTok-аккаунта с разной подачей.\n"
        "• Тестируй разные стили: строгий, мотивационный, с юмором.\n"
        "• Меняй заход: где-то упор на трейдинг, где-то на рефералку, где-то на свободу и образ жизни.\n\n"
        "Чем больше воронок, тем больше людей доходит до твоего бота и системы."
    ),
]

# ==========================
# КЛАВИАТУРЫ
# ==========================

def main_keyboard() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton("🎓 Обучение трейдингу"), KeyboardButton("📈 Сигналы"))
    kb.row(KeyboardButton("🚀 Обучение по трафику"), KeyboardButton("🤝 Партнёрская программа"))
    kb.row(KeyboardButton("💰 Купить доступ"), KeyboardButton("👤 Мой профиль"))
    return kb


def admin_keyboard() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton("👥 Все пользователи"), KeyboardButton("🧾 Покупки"))
    kb.row(KeyboardButton("🤝 Реферальные начисления"))
    return kb


def lessons_keyboard(lessons: List[Tuple[str, str]], prefix: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    for idx, (title, _) in enumerate(lessons):
        kb.insert(InlineKeyboardButton(text=title, callback_data=f"{prefix}:{idx}"))
    return kb

# ==========================
# УТИЛИТЫ
# ==========================

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


async def log_to_admin(text: str):
    try:
        await bot.send_message(ADMIN_ID, f"🛠 LOG:\n{text}")
    except Exception as e:
        logging.error(f"Не удалось отправить лог админу: {e}")


# ==========================
# TRONGRID: ПРОВЕРКА ОПЛАТЫ
# ==========================

async def fetch_trc20_transactions():
    """
    Забираем последние TRC20-транзакции для кошелька.
    """
    url = f"https://api.trongrid.io/v1/accounts/{WALLET_ADDRESS}/transactions/trc20?limit={TRON_TRANSACTIONS_LIMIT}"
    headers = {"TRON-PRO-API-KEY": TRONGRID_API_KEY}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                logging.error(f"TronGrid error status: {resp.status}")
                return []
            data = await resp.json()
    return data.get("data", [])


def parse_trx_amount(tx: dict):
    """
    Достаём сумму USDT с транзакции.
    """
    raw_value = tx.get("value") or tx.get("amount")
    if raw_value is None:
        return None
    try:
        amount = int(raw_value) / 1_000_000  # 6 знаков после запятой
        return amount
    except Exception:
        return None


def parse_trx_time(tx: dict):
    ts = tx.get("block_timestamp")
    if not ts:
        return ""
    # TronGrid даёт timestamp в миллисекундах
    dt = datetime.fromtimestamp(ts / 1000.0)
    return dt.strftime("%Y-%m-%d %H:%M")


def parse_trx_id(tx: dict):
    return tx.get("transaction_id") or tx.get("txID") or ""


# ==========================
# ХЕНДЛЕРЫ
# ==========================

@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    if not await anti_spam(message):
        return

    # Парсим /start ref_123
    referrer_id = None
    if message.get_args():
        args = message.get_args()
        if args.startswith("ref_"):
            try:
                candidate = int(args.replace("ref_", ""))
                if candidate != message.from_user.id and candidate > 0:
                    referrer_id = candidate
            except ValueError:
                referrer_id = None

    existing = get_user(message.from_user.id)
    if existing is None:
        save_user(message.from_user, referrer_id=referrer_id)
    else:
        # не затираем старого реферера
        _, _, _, _, _, old_ref, *_ = existing
        save_user(message.from_user, referrer_id=old_ref)

    text = (
        "👋 *Добро пожаловать в TradeX Partner Bot!*\n\n"
        "Здесь всё, чтобы ты смог:\n"
        "• разобраться в трейдинге\n"
        "• получать торговые сигналы\n"
        "• научиться лить трафик из TikTok в Telegram\n"
        "• зарабатывать на партнёрке *50% + 10%*\n\n"
        "Ты платишь за доступ к системе *один раз — 100$*,\n"
        "а дальше используешь и продукт, и реферальку.\n\n"
        "2–3 активных реферала уже могут вывести тебя в плюс.\n"
        "Выбирай действие ниже 👇"
    )
    await message.answer(text, reply_markup=main_keyboard())


# === ОБУЧЕНИЕ ТРЕЙДИНГУ ===

@dp.message_handler(Text(equals="🎓 Обучение трейдингу"))
async def trading_education(message: types.Message):
    if not await anti_spam(message):
        return

    text = (
        "🎓 *Обучение трейдингу*\n\n"
        "Это базовый курс, который даёт тебе фундамент:\n"
        "• что такое трейдинг\n"
        "• как не сливаться на эмоциях\n"
        "• как работать с сигналами\n"
        "• как выстроить путь к стабильности\n\n"
        "Выбери блок ниже 👇"
    )
    kb = lessons_keyboard(TRADING_LESSONS, prefix="trading")
    await message.answer(text, reply_markup=kb)


@dp.callback_query_handler(lambda c: c.data.startswith("trading:"))
async def trading_lesson_callback(call: types.CallbackQuery):
    idx = int(call.data.split(":")[1])
    title, body = TRADING_LESSONS[idx]

    # без Markdown — чтобы точно не ломалось из-за разметки
    await call.message.edit_text(
        f"{title}\n\n{body}",
        reply_markup=lessons_keyboard(TRADING_LESSONS, "trading"),
        parse_mode=None,
    )
    await call.answer()


# === ОБУЧЕНИЕ ТРАФИКУ ===

@dp.message_handler(Text(equals="🚀 Обучение по трафику"))
async def traffic_education(message: types.Message):
    if not await anti_spam(message):
        return

    text = (
        "🚀 *Обучение по переливу трафика из TikTok в Telegram*\n\n"
        "Ты узнаешь:\n"
        "• как оформить профиль TikTok под деньги\n"
        "• какие видео снимать\n"
        "• как вести людей в бота\n"
        "• как масштабировать трафик через несколько аккаунтов\n\n"
        "Выбери урок ниже 👇"
    )
    kb = lessons_keyboard(TRAFFIC_LESSONS, prefix="traffic")
    await message.answer(text, reply_markup=kb)


@dp.callback_query_handler(lambda c: c.data.startswith("traffic:"))
async def traffic_lesson_callback(call: types.CallbackQuery):
    idx = int(call.data.split(":")[1])
    title, body = TRAFFIC_LESSONS[idx]

    await call.message.edit_text(
        f"{title}\n\n{body}",
        reply_markup=lessons_keyboard(TRAFFIC_LESSONS, "traffic"),
        parse_mode=None,
    )
    await call.answer()


# === СИГНАЛЫ ===

@dp.message_handler(Text(equals="📈 Сигналы"))
async def signals_info(message: types.Message):
    if not await anti_spam(message):
        return

    text = (
        "📈 *Сигналы по трейдингу*\n\n"
        "После покупки доступа ты получаешь:\n"
        "• вход в закрытый канал с сигналами\n"
        "• уведомления по основным входам\n"
        "• понятную структуру работы по сигналам из обучения\n\n"
        "Чтобы попасть в закрытый канал — сначала оформи доступ через «💰 Купить доступ»."
    )
    await message.answer(text)


# === ПАРТНЁРКА ===

@dp.message_handler(Text(equals="🤝 Партнёрская программа"))
async def partner_program(message: types.Message):
    if not await anti_spam(message):
        return

    user_row = get_user(message.from_user.id)
    if user_row is None:
        save_user(message.from_user)
        user_row = get_user(message.from_user.id)

    ref_link = f"https://t.me/{(await bot.me).username}?start=ref_{message.from_user.id}"

    cursor.execute(
        "SELECT balance, level1_earned, level2_earned, total_withdrawn FROM users WHERE user_id = ?",
        (message.from_user.id,),
    )
    row = cursor.fetchone()
    if row:
        balance, lvl1, lvl2, withdrawn = row
    else:
        balance = lvl1 = lvl2 = withdrawn = 0.0

    text = (
        "🤝 *Партнёрская программа TradeX*\n\n"
        "Ты зарабатываешь вместе с системой:\n\n"
        f"• *{REF_L1_PERCENT}%* (≈ {PRODUCT_PRICE_USD * REF_L1_PERCENT / 100:.0f}$) "
        f"с каждого, кого приведёшь лично\n"
        f"• *{REF_L2_PERCENT}%* (≈ {PRODUCT_PRICE_USD * REF_L2_PERCENT / 100:.0f}$) "
        f"со второго уровня — людей, которых приводят твои рефералы\n\n"
        "Пример:\n"
        "— Ты привёл 3 человек → 3 × 50$ = 150$\n"
        "— Они привели ещё людей → ты докручиваешь по 10$ с каждого второго уровня.\n\n"
        f"Твоя реферальная ссылка:\n`{ref_link}`\n\n"
        "*Твоя статистика:*\n"
        f"• Баланс для вывода: *{balance:.2f}$*\n"
        f"• Заработано 1 уровень: *{lvl1:.2f}$*\n"
        f"• Заработано 2 уровень: *{lvl2:.2f}$*\n"
        f"• Уже выведено: *{withdrawn:.2f}$*\n\n"
        "Твоя задача — привести первых 1–3 активных людей.\n"
        "Дальше система начинает работать на тебя."
    )
    await message.answer(text)


# === ПОКУПКА ДОСТУПА (с уникальной суммой) ===

@dp.message_handler(Text(equals="💰 Купить доступ"))
async def buy_access(message: types.Message):
    if not await anti_spam(message):
        return

    user_row = get_user(message.from_user.id)
    if user_row is None:
        save_user(message.from_user)

    # генерируем уникальную сумму: 100.xxx
    tail = random.randint(1, 999)
    unique_amount = float(f"{PRODUCT_PRICE_USD}.{tail:03d}")

    create_purchase(message.from_user.id, PRODUCT_PRICE_USD, unique_amount)

    payment_text = (
        "💰 *Покупка доступа к системе TradeX*\n\n"
        "Один раз оплачиваешь доступ — и получаешь:\n"
        "• обучение по трейдингу\n"
        "• сигналы\n"
        "• обучение по переливу трафика из TikTok\n"
        "• партнёрскую программу 50% + 10%\n\n"
        f"Базовая цена: *{PRODUCT_PRICE_USD}$*\n"
        f"Твоя уникальная сумма для оплаты: *{unique_amount} USDT*\n\n"
        "⚠️ *Важно:* переведи ровно эту сумму до последнего знака.\n"
        "По ней бот будет искать именно твой платёж.\n\n"
        "Реквизиты:\n"
        f"• Сеть: TRC-20\n"
        f"• Кошелёк: `{WALLET_ADDRESS}`\n\n"
        "После отправки перевода нажми «✅ Я оплатил».\n"
        "Бот сначала попробует найти платёж автоматически.\n"
        f"Если что-то не так — можешь написать в поддержку: {SUPPORT_CONTACT}"
    )

    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton("✅ Я оплатил"), KeyboardButton("⬅️ В меню"))
    await message.answer(payment_text, reply_markup=kb)
    await log_to_admin(
        f"Новая заявка на оплату от {message.from_user.id}. Уникальная сумма: {unique_amount} USDT."
    )


@dp.message_handler(lambda m: m.text == "Я оплатил ✔️")
async def check_user_payment(message: types.Message):
    purchase = get_last_pending_purchase(message.from_user.id)

    if not purchase:
        await message.answer(
            "❗ У тебя нет активной заявки на оплату.\n"
            "Если ты уже платил — напиши админу и отправь скрин перевода."
        )
        return

    pid, uid, base_amount, unique_amount, status, created_at, confirmed_at, tx_amount, tx_time, tx_id = purchase

    # ✔️ ОБЯЗАТЕЛЬНО ВНУТРИ ФУНКЦИИ
    found = await check_payment_for_purchase(purchase)

    if found:
        await after_success_payment(purchase, manual_check=True)

    else:
        kb = ReplyKeyboardMarkup(resize_keyboard=True)
        kb.row(
            KeyboardButton("✔ Я оплатил"),
            KeyboardButton("← В меню")
        )
        await message.answer(
            "✖ Пока не вижу платёж с твоей уникальной суммой.\n"
            "Если ты только что отправил — подожди 1–2 минуты и нажми ещё раз.\n"
            f"Если есть сомнения — напиши в поддержку: {SUPPORT_CONTACT}",
            reply_markup=kb,
        )

    await log_to_admin(message)





@dp.message_handler(Text(equals="⬅️ В меню"))
async def back_to_menu(message: types.Message):
    if not await anti_spam(message):
        return
    await message.answer("🏠 Главное меню", reply_markup=main_keyboard())


# === ПРОФИЛЬ ===

@dp.message_handler(Text(equals="👤 Мой профиль"))
async def profile(message: types.Message):
    if not await anti_spam(message):
        return

    user_row = get_user(message.from_user.id)
    if user_row is None:
        save_user(message.from_user)
        user_row = get_user(message.from_user.id)

    (
        user_id,
        username,
        full_name,
        first_seen,
        last_active,
        referrer_id,
        balance,
        lvl1,
        lvl2,
        withdrawn,
        has_access,
    ) = user_row

    cursor.execute(
        "SELECT COUNT(*) FROM purchases WHERE user_id = ? AND status = 'confirmed'",
        (user_id,),
    )
    cnt_purchases = cursor.fetchone()[0]

    status_access = "🟢 Есть доступ к системе" if has_access else "🔴 Доступ не активирован"

    text = (
        "👤 *Твой профиль:*\n\n"
        f"ID: `{user_id}`\n"
        f"Username: @{username if username else '—'}\n"
        f"Имя: {full_name or '—'}\n\n"
        f"Первый вход: {first_seen}\n"
        f"Последняя активность: {last_active}\n\n"
        f"{status_access}\n"
        f"Оплаченных доступов: *{cnt_purchases}*\n\n"
        f"Баланс: *{balance:.2f}$*\n"
        f"1 уровень заработано: *{lvl1:.2f}$*\n"
        f"2 уровень заработано: *{lvl2:.2f}$*\n"
        f"Уже выведено: *{withdrawn:.2f}$*\n\n"
        f"Твой реферер: `{referrer_id}` (если 0 или None — значит, ты зашёл без приглашения).\n"
    )
    await message.answer(text)


# ==========================
# АДМИНКА
# ==========================

@dp.message_handler(commands=["admin"])
async def admin_panel(message: types.Message):
    if not is_admin(message.from_user.id):
        return await message.answer("🚫 У тебя нет доступа к админ-панели.")
    await message.answer("👨‍💻 Админ-панель", reply_markup=admin_keyboard())


@dp.message_handler(Text(equals="👥 Все пользователи"))
async def admin_all_users(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    cursor.execute(
        "SELECT user_id, username, full_name, first_seen, last_active, has_access "
        "FROM users ORDER BY first_seen DESC"
    )
    rows = cursor.fetchall()

    if not rows:
        return await message.answer("Пока нет пользователей.")

    text_parts = ["👥 *Все пользователи:*\n\n"]
    for uid, username, full_name, first_seen, last_active, has_access in rows:
        status_access = "🟢" if has_access else "🔴"
        text_parts.append(
            f"{status_access} ID: `{uid}`\n"
            f"Username: @{username if username else '—'}\n"
            f"Имя: {full_name or '—'}\n"
            f"Первый вход: {first_seen}\n"
            f"Последняя активность: {last_active}\n"
            "─────────────\n"
        )

    text = "".join(text_parts)
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i+4000])


@dp.message_handler(Text(equals="🧾 Покупки"))
async def admin_purchases(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    cursor.execute(
        "SELECT id, user_id, base_amount, unique_amount, status, created_at, confirmed_at, tx_amount, tx_time "
        "FROM purchases ORDER BY created_at DESC LIMIT 50"
    )
    rows = cursor.fetchall()

    if not rows:
        return await message.answer("Покупок пока нет.")

    text_parts = ["🧾 *Последние покупки:*\n\n"]
    for pid, uid, base_amount, unique_amount, status, created_at, confirmed_at, tx_amount, tx_time in rows:
        text_parts.append(
            f"ID покупки: `{pid}`\n"
            f"Пользователь: `{uid}`\n"
            f"Базовая сумма: {base_amount}$\n"
            f"Уникальная сумма: {unique_amount} USDT\n"
            f"Статус: *{status}*\n"
            f"Создано: {created_at}\n"
            f"Подтверждено: {confirmed_at or '—'}\n"
            f"Tx сумма: {tx_amount or 0} USDT\n"
            f"Tx время: {tx_time or '—'}\n"
            "─────────────\n"
        )

    text = "".join(text_parts)
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i+4000])


@dp.message_handler(Text(equals="🤝 Реферальные начисления"))
async def admin_ref_earnings(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    cursor.execute(
        """
        SELECT referrer_id, referred_id, level, bonus, created_at
        FROM referral_earnings
        ORDER BY created_at DESC
        LIMIT 50
        """
    )
    rows = cursor.fetchall()

    if not rows:
        return await message.answer("Пока не было реферальных начислений.")

    text_parts = ["🤝 *Реферальные начисления (последние 50):*\n\n"]
    for referrer_id, referred_id, level, bonus, created_at in rows:
        text_parts.append(
            f"Кому: `{referrer_id}` | Уровень: {level}\n"
            f"За кого: `{referred_id}`\n"
            f"Бонус: *{bonus:.2f}$*\n"
            f"Когда: {created_at}\n"
            "─────────────\n"
        )

    text = "".join(text_parts)
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i+4000])


# ==========================
# ЛОГИКА ПОСЛЕ ОПЛАТЫ
# ==========================

async def after_success_payment(purchase_row, manual_check: bool = False):
    """
    Вызывается, когда мы подтвердили платёж (авто или руками).
    Начисляет рефералку, даёт доступ, ссылку в канал.
    """
    pid, uid, base_amount, unique_amount, status, created_at, confirmed_at, tx_amount, tx_time, tx_id = purchase_row

    # ставим доступ
    set_access(uid, True)

    # реферальные начисления
    cursor.execute("SELECT referrer_id FROM users WHERE user_id = ?", (uid,))
    ref_row = cursor.fetchone()
    ref1 = ref_row[0] if ref_row else None

    if ref1:
        bonus1 = base_amount * REF_L1_PERCENT / 100
        add_referral_bonus(ref1, uid, level=1, bonus=bonus1)

        cursor.execute("SELECT referrer_id FROM users WHERE user_id = ?", (ref1,))
        ref2_row = cursor.fetchone()
        ref2 = ref2_row[0] if ref2_row else None

        if ref2:
            bonus2 = base_amount * REF_L2_PERCENT / 100
            add_referral_bonus(ref2, uid, level=2, bonus=bonus2)

    # отправляем ссылку в закрытый канал
    try:
        invite = await bot.create_chat_invite_link(CHANNEL_ID, member_limit=1)
        link_text = f"🔗 Твоя личная ссылка в закрытый канал:\n{invite.invite_link}"
    except Exception as e:
        logging.error(f"Ошибка создания инвайта: {e}")
        link_text = (
            "Не удалось автоматически создать ссылку в канал.\n"
            "Напиши админу, он выдаст доступ вручную."
        )

    try:
        await bot.send_message(
            uid,
            "✅ *Оплата найдена и подтверждена!*\n\n"
            "Тебе активирован доступ к системе:\n"
            "• обучение по трейдингу\n"
            "• сигналы\n"
            "• обучение по трафику\n"
            "• партнёрская программа 50% + 10%\n\n"
            + link_text,
        )
    except Exception:
        pass

    await log_to_admin(
        f"Успешная оплата. Пользователь {uid}, покупка {pid}, сумма {base_amount}$, уникальная {unique_amount}."
    )


async def check_payment_for_purchase(purchase_row):
    """
    Проверка конкретной заявки по данным с TronGrid.
    Возвращает True, если платёж найден и обновлён в БД.
    """
    pid, uid, base_amount, unique_amount, status, created_at, confirmed_at, tx_amount, tx_time, tx_id = purchase_row

    txs = await fetch_trc20_transactions()
    if not txs:
        return False

    created_dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M")

    for tx in txs:
        # токен должен быть USDT и получатель наш кошелёк
        token_info = tx.get("token_info") or {}
        symbol = token_info.get("symbol")
        to_addr = tx.get("to", "").lower()
        if symbol and symbol.upper() != "USDT":
            continue
        if to_addr and to_addr != WALLET_ADDRESS.lower():
            continue

        amount = parse_trx_amount(tx)
        if amount is None:
            continue

        # совпадение по уникальной сумме
        if abs(amount - unique_amount) > 0.0000001:
            continue

        tx_time_str = parse_trx_time(tx)
        tx_dt = None
        if tx_time_str:
            try:
                tx_dt = datetime.strptime(tx_time_str, "%Y-%m-%d %H:%M")
            except Exception:
                tx_dt = None

        # не принимаем транзакции, которые сильно старше заявки (анти-фрод)
        if tx_dt and tx_dt < created_dt:
            continue

        txid = parse_trx_id(tx)
        # проверяем, не использовался ли tx_id ранее
        if txid:
            cursor.execute(
                "SELECT COUNT(*) FROM purchases WHERE tx_id = ? AND status = 'confirmed'",
                (txid,),
            )
            if cursor.fetchone()[0] > 0:
                continue

        # если дошли сюда — считаем, что нашли платёж
        confirm_purchase_record(pid, amount, tx_time_str, txid)
        return True

    return False


# ==========================
# ФОНОВАЯ ПРОВЕРКА ПЛАТЕЖЕЙ
# ==========================

async def periodic_auto_check_payments():
    await bot.send_message(ADMIN_ID, "🔄 Авто-проверка платежей запущена.")
    while True:
        try:
            pending = get_all_pending_purchases()
            if pending:
                logging.info(f"Автопроверка платежей. Заявок в статусе pending: {len(pending)}")
            for purchase in pending:
                found = await check_payment_for_purchase(purchase)
                if found:
                    await after_success_payment(purchase, manual_check=False)
        except Exception as e:
            logging.error(f"Ошибка в periodic_auto_check_payments: {e}")
        await asyncio.sleep(PAYMENT_SCAN_INTERVAL)

# ==========================
# ЗАПУСК
# ==========================

import asyncio

async def on_startup(dispatcher):
    await log_to_admin("✅ Бот TradeX Partner Bot запущен.")
    asyncio.create_task(periodic_auto_check_payments())


if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
