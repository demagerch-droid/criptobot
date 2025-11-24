import os
import asyncio
import logging
import random
import sqlite3
import csv
from datetime import datetime, timedelta
from time import time

import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InputFile,
)
from aiogram.dispatcher.filters import Text

# ==========================
# НАСТРОЙКИ — МОЖНО ЧЕРЕЗ .env 
# ==========================

# 👉 Для безопасности лучше задать эти значения в переменных окружения на хостинге.
# Но я оставил твои реальные данные как значения по умолчанию, чтобы всё сразу работало.
# Если хочешь усилить безопасность — просто удали значения по умолчанию и используй только os.getenv(...).

BOT_TOKEN = os.getenv("BOT_TOKEN", "8330326273:AAEuWSwkqi7ypz1LZL4LXRr2jSMpKjGc36k")
ADMIN_ID = int(os.getenv("ADMIN_ID", "682938643"))

TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "b33b8d65-10c9-4f7b-99e0-ab47f3bbb60f")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "TSY9xf24bQ3Kbd1Njp2w4pEEoqJow1nfpr")

# Закрытый канал с сигналами
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1003464806734"))

# Новостной канал — ПОТОМ:
# когда создашь канал, вставь сюда его ID ИЛИ задай NEWS_CHANNEL_ID в env.
_news_env = os.getenv("NEWS_CHANNEL_ID", "0")
NEWS_CHANNEL_ID = int(_news_env) if _news_env and _news_env != "0" else None

DB_PATH = os.getenv("DB_PATH", "database.db")

# ЦЕНЫ
SUB_PRICE_USDT = 100.0                 # подписка на сигналы, 1 месяц
TRADING_COURSE_PRICE_USDT = 100.0      # обучение трейдингу
ARBITRAGE_COURSE_PRICE_USDT = 100.0    # курс по арбитражу трафика

# РЕФЕРАЛКА
REF_PERCENT = 40.0                     # % партнёрских
MIN_PAYOUT_USDT = 40.0                 # минимальная сумма на вывод

# ИНТЕРВАЛЫ ФОНОВЫХ ПРОЦЕССОВ
EXPIRE_CHECK_INTERVAL = 1800           # 30 минут — проверка истёкших подписок
PAYMENT_SCAN_INTERVAL = 60             # 1 минута — авто-проверка платежей

# Анти-спам для тяжёлых операций
PAYMENT_CHECK_COOLDOWN = 30            # секунд между ручными "🔄 Проверить оплату"
WITHDRAW_COOLDOWN = 60                 # секунд между запросами /withdraw

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("TradeXPartnerBot")

bot = Bot(token=BOT_TOKEN, parse_mode=types.ParseMode.HTML)
dp = Dispatcher(bot)

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

# Для анти-спама по пользователям
last_check_payment: dict[int, float] = {}
last_withdraw_request: dict[int, float] = {}

# ==========================
# БАЗА ДАННЫХ
# ==========================

# Пользователи
cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_seen TEXT,
        last_active TEXT,
        referrer_id INTEGER,
        utm_tag TEXT
    );
    """
)

# Подписки
cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS subscriptions(
        user_id INTEGER PRIMARY KEY,
        paid INTEGER,
        start_date TEXT,
        end_date TEXT,
        last_tx_amount REAL,
        last_tx_time TEXT
    );
    """
)

# Платежи
cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        product_type TEXT,
        amount REAL,
        tx_time TEXT,
        referrer_id INTEGER
    );
    """
)

# Выплаты партнёрам
cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS payouts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount REAL,
        status TEXT,
        created_at TEXT,
        paid_at TEXT,
        comment TEXT
    );
    """
)

conn.commit()

# ==========================
# КОНСТАНТЫ ПРОДУКТОВ
# ==========================

PRODUCT_SUBSCRIPTION = "subscription"
PRODUCT_TRADING_COURSE = "trading_course"
PRODUCT_ARBITRAGE_COURSE = "arbitrage_course"

PRODUCT_PRICES = {
    PRODUCT_SUBSCRIPTION: SUB_PRICE_USDT,
    PRODUCT_TRADING_COURSE: TRADING_COURSE_PRICE_USDT,
    PRODUCT_ARBITRAGE_COURSE: ARBITRAGE_COURSE_PRICE_USDT,
}

PRODUCT_TITLES = {
    PRODUCT_SUBSCRIPTION: "Подписка на сигналы (1 месяц)",
    PRODUCT_TRADING_COURSE: "Обучение трейдингу",
    PRODUCT_ARBITRAGE_COURSE: "Курс по арбитражу трафика",
}

# Временное хранение уникальной суммы: user_id -> dict(amount, product)
pending_payments: dict[int, dict] = {}


# ==========================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def save_user(user: types.User, referrer_id: int | None = None, utm_tag: str | None = None):
    """Создаём/обновляем пользователя и сохраняем, от кого он пришёл."""
    user_id = user.id
    username = user.username or ""
    now = now_str()

    cursor.execute("SELECT referrer_id, utm_tag FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()

    if row is None:
        cursor.execute(
            """
            INSERT INTO users (user_id, username, first_seen, last_active, referrer_id, utm_tag)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, username, now, now, referrer_id, utm_tag),
        )
    else:
        old_ref, old_utm = row
        final_ref = old_ref
        final_utm = old_utm

        # Если не было реферера и пришёл новый — сохраним
        if final_ref is None and referrer_id and referrer_id != user_id:
            final_ref = referrer_id
        # Если не было utm и пришёл новый — сохраним
        if (not final_utm) and utm_tag:
            final_utm = utm_tag

        cursor.execute(
            """
            UPDATE users
            SET username = ?, last_active = ?, referrer_id = ?, utm_tag = ?
            WHERE user_id = ?
            """,
            (username, now, final_ref, final_utm, user_id),
        )

    conn.commit()


def get_subscription(user_id: int):
    cursor.execute(
        """
        SELECT user_id, paid, start_date, end_date, last_tx_amount, last_tx_time
        FROM subscriptions
        WHERE user_id = ?
        """,
        (user_id,),
    )
    return cursor.fetchone()


def upsert_subscription_after_payment(user_id: int, amount: float):
    """Создаём/обновляем подписку после успешного платежа за подписку."""
    now = datetime.now()
    end = now + timedelta(days=30)

    cursor.execute(
        """
        INSERT OR REPLACE INTO subscriptions
        (user_id, paid, start_date, end_date, last_tx_amount, last_tx_time)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            1,
            now.strftime("%Y-%m-%d %H:%M"),
            end.strftime("%Y-%m-%d %H:%M"),
            amount,
            now.strftime("%Y-%m-%d %H:%M"),
        ),
    )
    conn.commit()


def record_payment(user_id: int, product_type: str, amount: float) -> int | None:
    """Записываем платёж в историю (payments) + считаем реферала."""
    now = now_str()
    cursor.execute("SELECT referrer_id FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    referrer_id = row[0] if row else None

    cursor.execute(
        """
        INSERT INTO payments (user_id, product_type, amount, tx_time, referrer_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, product_type, amount, now, referrer_id),
    )
    conn.commit()

    return referrer_id


def calculate_partner_stats(referrer_id: int) -> dict:
    """Считаем статистику партнёра: клики/рег/оплаты, суммы, баланс."""
    # Клики/регистрации — считаем по users
    cursor.execute(
        "SELECT COUNT(*) FROM users WHERE referrer_id = ?",
        (referrer_id,),
    )
    clicks = cursor.fetchone()[0] or 0
    registrations = clicks  # в телеге это, по сути, одно и то же

    # Оплаты и оборот
    cursor.execute(
        """
        SELECT COUNT(*), COALESCE(SUM(amount), 0)
        FROM payments
        WHERE referrer_id = ?
        """,
        (referrer_id,),
    )
    row = cursor.fetchone()
    payments_count = row[0] or 0
    turnover = row[1] or 0.0

    # Всего заработано: REF% от оборота
    total_earned = turnover * (REF_PERCENT / 100.0)

    # Выплачено
    cursor.execute(
        """
        SELECT COALESCE(SUM(amount), 0)
        FROM payouts
        WHERE user_id = ? AND status = 'paid'
        """,
        (referrer_id,),
    )
    paid_sum = cursor.fetchone()[0] or 0.0

    balance = total_earned - paid_sum

    # Статус по обороту
    if turnover >= 1000:
        rank = "Partner PRO"
    elif turnover >= 200:
        rank = "Pro"
    elif turnover > 0:
        rank = "Beginner"
    else:
        rank = "New"

    return {
        "clicks": clicks,
        "registrations": registrations,
        "payments_count": payments_count,
        "turnover": turnover,
        "total_earned": total_earned,
        "paid_sum": paid_sum,
        "balance": balance,
        "rank": rank,
    }


async def log_to_admin(text: str):
    try:
        await bot.send_message(ADMIN_ID, f"🛠 <b>Лог:</b>\n{text}")
    except Exception as e:
        logger.error(f"Не удалось отправить лог админу: {e}")


# ==========================
# TRONGRID CHECK
# ==========================

async def check_trx_payment(user_id: int) -> bool:
    """Проверяем, пришёл ли USDT с нужной уникальной суммой."""
    info = pending_payments.get(user_id)
    if not info:
        return False

    target_amount = info["amount"]

    url = f"https://api.trongrid.io/v1/accounts/{WALLET_ADDRESS}/transactions/trc20"
    headers = {"TRON-PRO-API-KEY": TRONGRID_API_KEY}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=15) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    await log_to_admin(
                        f"TronGrid ответил статусом {resp.status}. Тело: {body[:300]}"
                    )
                    return False
                data = await resp.json()
    except asyncio.TimeoutError:
        await log_to_admin("Timeout при запросе к TronGrid")
        return False
    except Exception as e:
        await log_to_admin(f"Ошибка запроса TronGrid: {e}")
        return False

    for tx in data.get("data", []):
        try:
            raw_value = tx.get("value") or tx.get("amount")
            if raw_value is None:
                continue
            amount = int(raw_value) / 1_000_000  # 6 знаков после запятой
            if abs(amount - target_amount) < 0.0000001:
                return True
        except Exception:
            continue

    return False


# ==========================
# КЛАВИАТУРЫ
# ==========================

def main_keyboard() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton("📈 Трейдинг"), KeyboardButton("💸 Заработок без трейдинга"))
    kb.row(KeyboardButton("📢 Новости проекта"), KeyboardButton("👤 Профиль"))
    kb.row(KeyboardButton("📞 Поддержка"))
    return kb


def trading_keyboard() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton("📌 О сигналах"), KeyboardButton("🔥 Почему это работает"))
    kb.row(KeyboardButton("💰 Оформить подписку"), KeyboardButton("🎓 Обучение трейдингу"))
    kb.row(KeyboardButton("⬅️ В главное меню"))
    return kb


def partner_keyboard() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton("🤝 Партнёрка 40%"), KeyboardButton("🔗 Моя ссылка"))
    kb.row(KeyboardButton("📊 Воронка и статистика"), KeyboardButton("💰 Баланс и выплаты"))
    kb.row(KeyboardButton("🎯 Курс по арбитражу"), KeyboardButton("🏆 Топ партнёров"))
    kb.row(KeyboardButton("⬅️ В главное меню"))
    return kb


def payment_keyboard() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton("🔄 Проверить оплату"))
    kb.row(KeyboardButton("⬅️ В главное меню"))
    return kb


def admin_keyboard() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton("👥 Все пользователи"), KeyboardButton("📊 Все подписчики"))
    kb.row(KeyboardButton("🔥 Активные подписчики"), KeyboardButton("⏳ Истёкшие"))
    kb.row(KeyboardButton("🧾 История платежей"), KeyboardButton("📤 Экспорт CSV"))
    kb.row(KeyboardButton("📈 Общая статистика"), KeyboardButton("📢 Инфо по рассылке"))
    kb.row(KeyboardButton("💼 Выплаты партнёрам"), KeyboardButton("🏆 Топ партнёров (админ)"))
    return kb


# ==========================
# /START + ОСНОВНОЙ ФЛОУ
# ==========================

@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    user = message.from_user
    args = message.get_args()

    ref_id = None
    utm_tag = None

    # Формат: ?start=12345 или 12345-tt-ua-r1 (и т.п.)
    if args:
        parts = args.split("-", 1)
        try:
            ref_id = int(parts[0])
        except ValueError:
            ref_id = None
        if len(parts) > 1:
            utm_tag = parts[1][:64]

    save_user(user, referrer_id=ref_id, utm_tag=utm_tag)

    row = get_subscription(user.id)
    now = datetime.now()

    extra = ""
    if row:
        _, paid, _, end_date, last_tx_amount, last_tx_time = row
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d %H:%M")
        except Exception:
            end_dt = now
        if paid == 1 and end_dt > now:
            extra = (
                f"\n\n🔥 У тебя уже есть <b>активная подписка</b>\n"
                f"Действует до: <b>{end_date}</b>\n"
                f"Последняя оплата: <b>{last_tx_amount} USDT</b> ({last_tx_time})\n"
            )

    text = (
        "👋 <b>Добро пожаловать в TradeX Partner Bot</b>\n\n"
        "Здесь два направления:\n"
        "• <b>📈 Трейдинг</b> — готовые сигналы и обучение трейдингу\n"
        "• <b>💸 Заработок без трейдинга</b> — партнёрка с выплатами 40% за каждую продажу\n\n"
        "Тебе не обязательно быть трейдером, чтобы зарабатывать на крипте.\n"
        "Можно просто приводить людей и получать свой процент.\n"
        f"{extra}\n"
        "Выбирай, с чего начнём 👇"
    )
    await message.answer(text, reply_markup=main_keyboard())


# ==========================
# БЛОК ТРЕЙДИНГА
# ==========================

@dp.message_handler(Text(equals="📈 Трейдинг"))
async def menu_trading(message: types.Message):
    text = (
        "📈 <b>Раздел: Трейдинг</b>\n\n"
        "Здесь всё для тех, кто хочет зарабатывать на рынке с помощью сигналов и системного подхода.\n\n"
        "Выбери действие ниже 👇"
    )
    await message.answer(text, reply_markup=trading_keyboard())


@dp.message_handler(Text(equals="📌 О сигналах"))
async def about_signals(message: types.Message):
    text = (
        "📌 <b>О сигналах</b>\n\n"
        "• Понятные точки входа и выхода\n"
        "• Работаем с USDT-парами\n"
        "• Чёткие Stop Loss и Take Profit\n"
        "• Минимум воды, максимум практики\n\n"
        "Подписка даёт доступ в закрытый канал, где ты просто получаешь готовые идеи и можешь применять их в своём темпе."
    )
    await message.answer(text)


@dp.message_handler(Text(equals="🔥 Почему это работает"))
async def why_it_works(message: types.Message):
    text = (
        "🔥 <b>Почему это работает</b>\n\n"
        "95% людей на рынке теряют деньги, потому что действуют хаотично.\n"
        "У них нет системы, дисциплины и чётких правил.\n\n"
        "Наша задача — дать тебе структуру:\n"
        "• готовые сигналы\n"
        "• понятные правила входа\n"
        "• чёткость по риску\n\n"
        "Тебе не нужно быть «гуру трейдинга».\n"
        "Достаточно уметь следовать простой логике.\n"
    )
    await message.answer(text)


async def send_warmup_and_payment(message: types.Message, product_type: str):
    """Укрепляющий прогрев (3 шага) + выдача реквизитов оплаты."""
    user_id = message.from_user.id
    price = PRODUCT_PRICES[product_type]
    title = PRODUCT_TITLES[product_type]

    # Прогрев 1
    text1 = (
        "1️⃣ <b>Осознанный шаг</b>\n\n"
        "Большинство людей мечтают о свободе и деньгах, но продолжают жить по инерции.\n"
        "Ты уже отличаешься от них хотя бы тем, что ищешь возможности и дошёл до этого шага."
    )
    await message.answer(text1)
    await asyncio.sleep(1.2)

    # Прогрев 2
    text2 = (
        "2️⃣ <b>Готовая система вместо хаоса</b>\n\n"
        "Крипта может быть либо казино, либо инструментом.\n"
        "Когда есть сигналы и обучение — у тебя появляется опора, а не просто догадки.\n"
    )
    await message.answer(text2)
    await asyncio.sleep(1.2)

    # Прогрев 3
    text3 = (
        "3️⃣ <b>Решение, которое меняет траекторию</b>\n\n"
        "Сейчас ты стоишь между «оставить всё как есть» и «дать себе шанс». "
        "Решение всегда за тобой.\n\n"
        "Если готов сделать шаг — просто оформи оплату ниже 👇"
    )
    await message.answer(text3)
    await asyncio.sleep(1.2)

    # Уникальная сумма
    unique_tail = random.randint(1, 999)
    unique_price = float(f"{price:.0f}.{unique_tail:03d}")

    pending_payments[user_id] = {"amount": unique_price, "product": product_type}

    text_pay = (
        f"💳 <b>Оплата: {title}</b>\n\n"
        f"1️⃣ Отправь <b>РОВНО</b> <code>{unique_price}</code> USDT (TRC-20)\n"
        f"2️⃣ На адрес кошелька:\n<code>{WALLET_ADDRESS}</code>\n\n"
        "⚠️ <b>Важно:</b> сумма должна совпасть до последнего знака — это нужно для автоматической идентификации платежа.\n\n"
        "После отправки USDT нажми «🔄 Проверить оплату».\n"
        "Если что-то пошло не так — всегда можно написать админу."
    )
    await message.answer(text_pay, reply_markup=payment_keyboard())


@dp.message_handler(Text(equals="💰 Оформить подписку"))
async def buy_subscription(message: types.Message):
    await send_warmup_and_payment(message, PRODUCT_SUBSCRIPTION)


@dp.message_handler(Text(equals="🎓 Обучение трейдингу"))
async def buy_trading_course(message: types.Message):
    await send_warmup_and_payment(message, PRODUCT_TRADING_COURSE)


# ==========================
# БЛОК ПАРТНЁРКИ
# ==========================

@dp.message_handler(Text(equals="💸 Заработок без трейдинга"))
async def menu_partner(message: types.Message):
    text = (
        "💸 <b>Заработок без трейдинга</b>\n\n"
        "Тебе не обязательно сидеть у графиков, чтобы зарабатывать на этом проекте.\n\n"
        f"• Все продукты стоят <b>100 USDT</b>\n"
        f"• Твой процент с каждой оплаты — <b>{REF_PERCENT:.0f}%</b> (то есть <b>40 USDT</b>)\n"
        "• Ты просто приводишь людей, бот делает всё остальное.\n\n"
        "Здесь ты найдёшь свою ссылку, статистику и возможность запросить вывод."
    )
    await message.answer(text, reply_markup=partner_keyboard())


@dp.message_handler(Text(equals="🤝 Партнёрка 40%"))
async def partner_info(message: types.Message):
    uid = message.from_user.id
    me = await bot.get_me()
    deeplink = f"https://t.me/{me.username}?start={uid}"

    text = (
        "🤝 <b>Партнёрская программа 40%</b>\n\n"
        "Ты можешь зарабатывать на этом боте, даже если вообще не торгуешь.\n\n"
        "📌 Как это работает:\n"
        "• Ты даёшь людям свою ссылку\n"
        "• Они заходят в бота, покупают подписку или обучение\n"
        f"• С каждого платежа ты получаешь <b>{REF_PERCENT:.0f}%</b> (то есть <b>40 USDT</b> с 100 USDT)\n\n"
        "🎯 Преимущество: система работает 24/7, люди покупают — а ты просто видишь, как растёт баланс.\n\n"
        "Твоя личная ссылка:\n"
        f"<code>{deeplink}</code>\n\n"
        "Можешь прикручивать её к TikTok, Telegram, YouTube, арбитражу и чему угодно."
    )
    await message.answer(text)


@dp.message_handler(Text(equals="🔗 Моя ссылка"))
async def partner_link(message: types.Message):
    uid = message.from_user.id
    me = await bot.get_me()
    deeplink = f"https://t.me/{me.username}?start={uid}"

    text = (
        "🔗 <b>Твоя партнёрская ссылка</b>\n\n"
        f"<code>{deeplink}</code>\n\n"
        "Совет: добавь её в описание профиля, под роликами, в шапку канала.\n"
        "Каждый человек, который оплатит через неё — это +40 USDT к твоему потенциальному доходу."
    )
    await message.answer(text)


@dp.message_handler(Text(equals="📊 Воронка и статистика"))
async def partner_funnel(message: types.Message):
    uid = message.from_user.id
    stats = calculate_partner_stats(uid)

    text = (
        "📊 <b>Твоя воронка и статистика</b>\n\n"
        f"Статус: <b>{stats['rank']}</b>\n\n"
        f"Трафик (люди, которые пришли по ссылке): <b>{stats['clicks']}</b>\n"
        f"Регистрации (нажали /start): <b>{stats['registrations']}</b>\n"
        f"Оплаты: <b>{stats['payments_count']}</b>\n"
        f"Оборот по твоим рефералам: <b>{stats['turnover']:.2f} USDT</b>\n\n"
        f"Всего заработано (теоретически): <b>{stats['total_earned']:.2f} USDT</b>\n"
        f"Выплачено: <b>{stats['paid_sum']:.2f} USDT</b>\n"
        f"Текущий баланс: <b>{stats['balance']:.2f} USDT</b>\n\n"
        "Чем больше людей ты приводишь — тем стабильнее становится твой доход.\n"
        "Умные используют это как отдельную систему заработка."
    )
    await message.answer(text)


@dp.message_handler(Text(equals="💰 Баланс и выплаты"))
async def partner_balance(message: types.Message):
    uid = message.from_user.id
    stats = calculate_partner_stats(uid)

    text = (
        "💰 <b>Твой партнёрский баланс</b>\n\n"
        f"Статус: <b>{stats['rank']}</b>\n\n"
        f"Всего заработано: <b>{stats['total_earned']:.2f} USDT</b>\n"
        f"Выплачено: <b>{stats['paid_sum']:.2f} USDT</b>\n"
        f"Текущий баланс: <b>{stats['balance']:.2f} USDT</b>\n\n"
        f"Минимальная сумма на вывод: <b>{MIN_PAYOUT_USDT:.2f} USDT</b>\n\n"
        "Чтобы запросить выплату, отправь команду в формате:\n"
        "<code>/withdraw ТВОЙ_TRON_WALLET</code>\n"
        "Например:\n"
        "<code>/withdraw TSyZ...123</code>\n\n"
        "После этого админ проверит данные и свяжется с тобой."
    )
    await message.answer(text)


@dp.message_handler(commands=["withdraw"])
async def cmd_withdraw(message: types.Message):
    uid = message.from_user.id
    now_ts = time()
    last = last_withdraw_request.get(uid, 0.0)
    if now_ts - last < WITHDRAW_COOLDOWN:
        return await message.reply(
            "⏳ Ты слишком часто запрашиваешь вывод.\n"
            "Подожди немного и попробуй снова."
        )
    last_withdraw_request[uid] = now_ts

    stats = calculate_partner_stats(uid)
    args = message.get_args().strip()

    if not args:
        return await message.reply(
            "Чтобы запросить выплату, укажи свой TRC20 USDT-кошелёк.\n"
            "Пример:\n"
            "<code>/withdraw TSyZ...123</code>"
        )

    if stats["balance"] < MIN_PAYOUT_USDT:
        return await message.reply(
            f"Сейчас на балансе <b>{stats['balance']:.2f} USDT</b>, этого недостаточно для вывода.\n"
            f"Минимальная сумма: <b>{MIN_PAYOUT_USDT:.2f} USDT</b>."
        )

    amount = stats["balance"]

    cursor.execute(
        """
        INSERT INTO payouts (user_id, amount, status, created_at, paid_at, comment)
        VALUES (?, ?, 'pending', ?, NULL, ?)
        """,
        (uid, amount, now_str(), args),
    )
    conn.commit()

    await message.reply(
        "✅ Запрос на выплату отправлен.\n"
        "Админ проверит данные и свяжется с тобой по поводу перевода."
    )

    await log_to_admin(
        f"ЗАПРОС ВЫПЛАТЫ\n"
        f"Партнёр: {uid}\n"
        f"Сумма: {amount:.2f} USDT\n"
        f"Кошелёк: {args}"
    )


@dp.message_handler(Text(equals="🎯 Курс по арбитражу"))
async def buy_arbitrage_course(message: types.Message):
    await send_warmup_and_payment(message, PRODUCT_ARBITRAGE_COURSE)


@dp.message_handler(Text(equals="🏆 Топ партнёров"))
async def partner_top(message: types.Message):
    # топ партнёров по обороту
    cursor.execute(
        """
        SELECT referrer_id, COALESCE(SUM(amount), 0) as total
        FROM payments
        WHERE referrer_id IS NOT NULL
        GROUP BY referrer_id
        ORDER BY total DESC
        LIMIT 10
        """
    )
    rows = cursor.fetchall()
    if not rows:
        return await message.answer("Пока ещё нет данных по партнёрам.")

    text = "🏆 <b>Топ партнёров</b>\n\n"
    place = 1
    for ref_id, total in rows:
        stats = calculate_partner_stats(ref_id)
        user_tag = f"<code>{ref_id}</code>"
        text += (
            f"{place}. {user_tag} — оборот: <b>{total:.2f} USDT</b>, "
            f"заработано: <b>{stats['total_earned']:.2f} USDT</b>\n"
        )
        place += 1

    await message.answer(text)


# ==========================
# ПРОФИЛЬ И ПОДДЕРЖКА
# ==========================

@dp.message_handler(Text(equals="👤 Профиль"))
async def profile(message: types.Message):
    uid = message.from_user.id
    row = get_subscription(uid)
    now = datetime.now()

    if row:
        _, paid, start_date, end_date, last_tx_amount, last_tx_time = row
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d %H:%M")
        except Exception:
            end_dt = now
        active = paid == 1 and end_dt > now
        days_left = max((end_dt - now).days, 0)
        sub_status = "🟢 Активна" if active else "🔴 Не активна"
    else:
        paid = 0
        start_date = "—"
        end_date = "—"
        last_tx_amount = 0
        last_tx_time = "—"
        days_left = 0
        sub_status = "🔴 Нет подписки"

    stats = calculate_partner_stats(uid)

    text = (
        "👤 <b>Твой профиль</b>\n\n"
        f"ID: <code>{uid}</code>\n"
        f"Статус подписки: {sub_status}\n"
        f"Начало: <b>{start_date}</b>\n"
        f"Окончание: <b>{end_date}</b>\n"
        f"Осталось дней: <b>{days_left}</b>\n"
        f"Последний платёж: <b>{last_tx_amount} USDT</b> ({last_tx_time})\n\n"
        f"🤝 <b>Партнёрский блок</b>\n"
        f"Статус: <b>{stats['rank']}</b>\n"
        f"Оборот по рефералам: <b>{stats['turnover']:.2f} USDT</b>\n"
        f"Всего заработано: <b>{stats['total_earned']:.2f} USDT</b>\n"
        f"Выплачено: <b>{stats['paid_sum']:.2f} USDT</b>\n"
        f"Баланс: <b>{stats['balance']:.2f} USDT</b>\n\n"
        "Если хочешь — можешь полностью жить на пассивных выплатах от партнёрки.\n"
        "Всё начинается с первой ссылки."
    )
    await message.answer(text)


@dp.message_handler(Text(equals="📞 Поддержка"))
async def support(message: types.Message):
    text = (
        "📞 <b>Поддержка</b>\n\n"
        "Если есть вопросы по оплате, доступу в канал, партнёрке или курсам — пиши админу:\n"
        "<b>@your_support_username</b>\n\n"
        "Если оплата прошла, а бот не подтянул её — не переживай, всё можно проверить вручную."
    )
    await message.answer(text)


@dp.message_handler(Text(equals="📢 Новости проекта"))
async def news(message: types.Message):
    if not NEWS_CHANNEL_ID:
        return await message.answer(
            "Скоро здесь появится новостной канал с обновлениями по проекту.\n"
            "Ты всегда сможешь быть в курсе изменений."
        )

    try:
        invite = await bot.create_chat_invite_link(NEWS_CHANNEL_ID, member_limit=1)
        await message.answer(
            "📢 <b>Новости проекта</b>\n\n"
            "Здесь мы делимся обновлениями по боту, стратегиям, партнёрке и новым возможностям.\n\n"
            f"Вход в новостной канал:\n{invite.invite_link}"
        )
    except Exception:
        await message.answer(
            "Пока не удалось создать ссылку в канал.\n"
            "Если тебе важны новости — напиши админу."
        )


# ==========================
# ОПЛАТА (ПРОВЕРКА)
# ==========================

@dp.message_handler(Text(equals="🔄 Проверить оплату"))
async def check_payment_button(message: types.Message):
    uid = message.from_user.id

    if uid not in pending_payments:
        return await message.answer(
            "У тебя нет ожидающего платежа.\n"
            "Если хочешь оформить покупку — выбери продукт в меню."
        )

    # Анти-спам: не даём жать «Проверить оплату» каждые 2 секунды
    now_ts = time()
    last = last_check_payment.get(uid, 0.0)
    if now_ts - last < PAYMENT_CHECK_COOLDOWN:
        remain = int(PAYMENT_CHECK_COOLDOWN - (now_ts - last))
        return await message.answer(
            f"⏳ Проверка уже выполнялась недавно.\n"
            f"Подожди ещё <b>{remain} сек.</b> перед следующей попыткой."
        )
    last_check_payment[uid] = now_ts

    await message.answer("⏳ Проверяю оплату в сети TRON, подожди 5–15 секунд...")

    if await check_trx_payment(uid):
        info = pending_payments.get(uid)
        amount = info["amount"]
        product = info["product"]

        # Сохраняем платёж
        referrer_id = record_payment(uid, product, amount)

        # Если это подписка — обновляем таблицу подписок и добавляем в канал
        if product == PRODUCT_SUBSCRIPTION:
            upsert_subscription_after_payment(uid, amount)
            try:
                invite = await bot.create_chat_invite_link(CHANNEL_ID, member_limit=1)
                await message.answer(
                    "✅ Оплата за подписку подтверждена!\n"
                    "Вот твоя ссылка в закрытый канал с сигналами:\n"
                    f"{invite.invite_link}"
                )
            except Exception as e:
                await message.answer(
                    "Оплата прошла, но не удалось автоматически создать ссылку в канал.\n"
                    "Напиши админу — он вручную выдаст доступ."
                )
                await log_to_admin(f"Ошибка создания инвайта для {uid}: {e}")
        elif product == PRODUCT_TRADING_COURSE:
            await message.answer(
                "✅ Оплата за <b>обучение трейдингу</b> подтверждена!\n\n"
                "Скоро админ свяжется с тобой и выдаст доступ к материалам.\n"
                "Если хочешь ускорить — сам напиши в поддержку."
            )
        elif product == PRODUCT_ARBITRAGE_COURSE:
            await message.answer(
                "✅ Оплата за <b>курс по арбитражу трафика</b> подтверждена!\n\n"
                "Скоро админ свяжется с тобой и выдаст доступ к обучению.\n"
                "Если хочешь ускорить — сам напиши в поддержку."
            )

        # Уведомление партнёру
        if referrer_id:
            stats = calculate_partner_stats(referrer_id)
            try:
                await bot.send_message(
                    referrer_id,
                    "🔥 <b>Новый заработок по партнёрке!</b>\n\n"
                    f"Кто-то оформил: <b>{PRODUCT_TITLES[product]}</b>\n"
                    f"Сумма: <b>{amount:.2f} USDT</b>\n\n"
                    f"Твой общий заработок: <b>{stats['total_earned']:.2f} USDT</b>\n"
                    f"Текущий баланс: <b>{stats['balance']:.2f} USDT</b>\n\n"
                    "Ещё один шаг к тому, чтобы деньги работали вместо тебя."
                )
            except Exception:
                pass

        await log_to_admin(f"PAYMENT SUCCESS: user {uid}, product {product}, amount {amount}")
        pending_payments.pop(uid, None)
    else:
        await message.answer(
            "❌ Платёж пока не найден.\n\n"
            "Если ты только что отправил USDT — подожди 1–2 минуты и нажми ещё раз.\n"
            "Если уверен, что всё сделал правильно — напиши админу, всё проверим."
        )


@dp.message_handler(Text(equals="⬅️ В главное меню"))
async def back_to_main(message: types.Message):
    await message.answer("🏠 Главное меню:", reply_markup=main_keyboard())


# ==========================
# АДМИН-ПАНЕЛЬ
# ==========================

@dp.message_handler(commands=["admin"])
async def admin_panel(message: types.Message):
    if not is_admin(message.from_user.id):
        return await message.answer("🚫 У тебя нет доступа.")
    await message.answer("👨‍💻 <b>Админ-панель</b>", reply_markup=admin_keyboard())


@dp.message_handler(Text(equals="👥 Все пользователи"))
async def admin_all_users(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    cursor.execute("SELECT user_id, username, first_seen, last_active, referrer_id, utm_tag FROM users")
    rows = cursor.fetchall()
    if not rows:
        return await message.answer("Пока ни один пользователь не запускал бота.")

    text = "👥 <b>Все пользователи:</b>\n\n"
    chunks = []
    for user_id, username, first_seen, last_active, referrer_id, utm_tag in rows:
        text += (
            f"ID: <code>{user_id}</code>\n"
            f"Username: @{username if username else 'нет'}\n"
            f"Впервые: {first_seen}\n"
            f"Активность: {last_active}\n"
            f"Реферер: {referrer_id if referrer_id else 'нет'}\n"
            f"UTM: {utm_tag if utm_tag else 'нет'}\n"
            "───────────────\n"
        )
        if len(text) > 3500:
            chunks.append(text)
            text = ""
    if text:
        chunks.append(text)

    for chunk in chunks:
        await message.answer(chunk)


@dp.message_handler(Text(equals="📊 Все подписчики"))
async def admin_all_subs(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    cursor.execute("SELECT * FROM subscriptions")
    cursor.execute("""
    SELECT
        user_id,
        paid,
        start_date,
        end_date,
        last_tx_amount,
        last_tx_time
    FROM subscriptions
""")
rows = cursor.fetchall()


    if not rows:
        return await message.answer("Пока нет подписчиков.")

    text = "📊 <b>Все подписчики:</b>\n\n"
    chunks = []
    for user_id, paid, start_date, end_date, last_tx_amount, last_tx_time in rows:
        status = "🟢 Активна" if paid == 1 else "🔴 Не активна"
        text += (
            f"ID: <code>{user_id}</code>\n"
            f"Статус: {status}\n"
            f"Старт: {start_date}\n"
            f"Конец: {end_date}\n"
            f"Последний платёж: {last_tx_amount} USDT ({last_tx_time})\n"
            "───────────────\n"
        )
        if len(text) > 3500:
            chunks.append(text)
            text = ""
    if text:
        chunks.append(text)

    for chunk in chunks:
        await message.answer(chunk)


@dp.message_handler(Text(equals="🔥 Активные подписчики"))
async def admin_active_subs(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    now_dt = datetime.now()
    cursor.execute("SELECT * FROM subscriptions WHERE paid = 1")
    rows = cursor.fetchall()
    active = []
    for user_id, paid, start_date, end_date, last_tx_amount, last_tx_time in rows:
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d %H:%M")
        except Exception:
            continue
        if end_dt > now_dt:
            active.append((user_id, end_date, last_tx_amount))

    if not active:
        return await message.answer("Нет активных подписчиков.")

    text = "🔥 <b>Активные подписчики:</b>\n\n"
    for user_id, end_date, last_tx_amount in active:
        text += (
            f"ID: <code>{user_id}</code>\n"
            f"Доступ до: {end_date}\n"
            f"Последний платёж: {last_tx_amount} USDT\n"
            "───────────────\n"
        )

    await message.answer(text)


@dp.message_handler(Text(equals="⏳ Истёкшие"))
async def admin_expired_subs(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    now_dt = datetime.now()
    cursor.execute("SELECT * FROM subscriptions")
    rows = cursor.fetchall()

    expired = []
    for user_id, paid, start_date, end_date, last_tx_amount, last_tx_time in rows:
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d %H:%M")
        except Exception:
            continue
        if end_dt < now_dt:
            expired.append((user_id, start_date, end_date))

    if not expired:
        return await message.answer("Истёкших подписок нет.")

    text = "⏳ <b>Истёкшие подписки:</b>\n\n"
    for user_id, start_date, end_date in expired:
        text += (
            f"ID: <code>{user_id}</code>\n"
            f"Старт: {start_date}\n"
            f"Истекла: {end_date}\n"
            "───────────────\n"
        )

    await message.answer(text)


@dp.message_handler(Text(equals="🧾 История платежей"))
async def admin_payments(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    cursor.execute(
        """
        SELECT user_id, product_type, amount, tx_time, referrer_id
        FROM payments
        ORDER BY tx_time DESC
        LIMIT 100
        """
    )
    rows = cursor.fetchall()
    if not rows:
        return await message.answer("История платежей пуста.")

    text = "🧾 <b>Последние платежи:</b>\n\n"
    for user_id, product_type, amount, tx_time, referrer_id in rows:
        text += (
            f"Пользователь: <code>{user_id}</code>\n"
            f"Продукт: {PRODUCT_TITLES.get(product_type, product_type)}\n"
            f"Сумма: {amount:.2f} USDT\n"
            f"Время: {tx_time}\n"
            f"Реферер: {referrer_id if referrer_id else 'нет'}\n"
            "───────────────\n"
        )

    await message.answer(text)


@dp.message_handler(Text(equals="📤 Экспорт CSV"))
async def admin_export_csv(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    cursor.execute("SELECT * FROM subscriptions")
    rows = cursor.fetchall()
    if not rows:
        return await message.answer("Нет данных для экспорта.")

    filename = "subscriptions_export.csv"
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["user_id", "paid", "start_date", "end_date", "last_tx_amount", "last_tx_time"]
        )
        for row in rows:
            writer.writerow(row)

    await message.answer_document(InputFile(filename), caption="Экспорт подписчиков.")


@dp.message_handler(Text(equals="📈 Общая статистика"))
async def admin_stats(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0] or 0

    cursor.execute("SELECT COUNT(*) FROM subscriptions WHERE paid = 1")
    total_paid = cursor.fetchone()[0] or 0

    cursor.execute("SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM payments")
    row = cursor.fetchone()
    total_payments = row[0] or 0
    total_amount = row[1] or 0.0

    cursor.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM payments WHERE product_type = ?",
        (PRODUCT_SUBSCRIPTION,),
    )
    subs_amount = cursor.fetchone()[0] or 0.0

    text = (
        "📈 <b>Общая статистика проекта</b>\n\n"
        f"👥 Пользователей: <b>{total_users}</b>\n"
        f"💳 Активных подписок: <b>{total_paid}</b>\n"
        f"🧾 Всего платежей: <b>{total_payments}</b>\n"
        f"💰 Общий оборот: <b>{total_amount:.2f} USDT</b>\n"
        f"📈 Оборот по подпискам: <b>{subs_amount:.2f} USDT</b>\n"
    )
    await message.answer(text)


@dp.message_handler(Text(equals="📢 Инфо по рассылке"))
async def admin_broadcast_info(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    text = (
        "📢 <b>Рассылка</b>\n\n"
        "Чтобы отправить сообщение всем пользователям, используй команду:\n"
        "<code>/broadcast Текст сообщения</code>\n\n"
        "Бот постарается доставить сообщение каждому пользователю из базы."
    )
    await message.answer(text)


@dp.message_handler(commands=["broadcast"])
async def cmd_broadcast(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    text = message.get_args().strip()
    if not text:
        return await message.reply("После команды /broadcast напиши текст рассылки.")

    cursor.execute("SELECT user_id FROM users")
    rows = cursor.fetchall()
    sent = 0
    for (user_id,) in rows:
        try:
            await bot.send_message(user_id, text)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            continue

    await message.answer(f"✅ Рассылка завершена. Отправлено: {sent} пользователям.")


@dp.message_handler(Text(equals="💼 Выплаты партнёрам"))
async def admin_payouts(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    cursor.execute(
        """
        SELECT id, user_id, amount, status, created_at, paid_at, comment
        FROM payouts
        ORDER BY created_at DESC
        LIMIT 50
        """
    )
    rows = cursor.fetchall()
    if not rows:
        return await message.answer("Пока нет заявок на выплаты.")

    text = "💼 <b>Заявки на выплаты партнёрам:</b>\n\n"
    for pid, uid, amount, status, created_at, paid_at, comment in rows:
        text += (
            f"ID заявки: <b>{pid}</b>\n"
            f"Партнёр: <code>{uid}</code>\n"
            f"Сумма: <b>{amount:.2f} USDT</b>\n"
            f"Статус: <b>{status}</b>\n"
            f"Создано: {created_at}\n"
            f"Оплачено: {paid_at if paid_at else '—'}\n"
            f"Кошелёк: {comment}\n"
            "───────────────\n"
        )

    text += (
        "\nЧтобы отметить выплату выполненной, используй команду:\n"
        "<code>/payout_done ID_ЗАЯВКИ</code>\n"
    )
    await message.answer(text)


@dp.message_handler(commands=["payout_done"])
async def cmd_payout_done(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    args = message.get_args().strip()
    if not args:
        return await message.reply("Укажи ID заявки: <code>/payout_done 1</code>")

    try:
        payout_id = int(args)
    except ValueError:
        return await message.reply("ID должен быть числом.")

    cursor.execute("SELECT user_id, amount, status FROM payouts WHERE id = ?", (payout_id,))
    row = cursor.fetchone()
    if not row:
        return await message.reply("Заявка с таким ID не найдена.")

    uid, amount, status = row
    if status == "paid":
        return await message.reply("Эта заявка уже отмечена как выплаченная.")

    cursor.execute(
        """
        UPDATE payouts
        SET status = 'paid', paid_at = ?
        WHERE id = ?
        """,
        (now_str(), payout_id),
    )
    conn.commit()

    await message.reply(f"✅ Заявка {payout_id} отмечена как выплаченная.")
    try:
        await bot.send_message(
            uid,
            f"💸 <b>Выплата подтверждена!</b>\n\n"
            f"Тебе отправлено: <b>{amount:.2f} USDT</b>\n"
            "Спасибо за партнёрство. Продолжай — и ты увидишь, как это масштабируется."
        )
    except Exception:
        pass


@dp.message_handler(Text(equals="🏆 Топ партнёров (админ)"))
async def admin_top_partners(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    cursor.execute(
        """
        SELECT referrer_id, COALESCE(SUM(amount), 0) as total
        FROM payments
        WHERE referrer_id IS NOT NULL
        GROUP BY referrer_id
        ORDER BY total DESC
        LIMIT 20
        """
    )
    rows = cursor.fetchall()
    if not rows:
        return await message.answer("Нет данных по партнёрам.")

    text = "🏆 <b>Топ партнёров (по обороту)</b>\n\n"
    place = 1
    for ref_id, total in rows:
        stats = calculate_partner_stats(ref_id)
        text += (
            f"{place}. ID: <code>{ref_id}</code> — оборот: <b>{total:.2f} USDT</b>, "
            f"заработано: <b>{stats['total_earned']:.2f} USDT</b>, "
            f"выплачено: <b>{stats['paid_sum']:.2f} USDT</b>\n"
        )
        place += 1

    await message.answer(text)


# ==========================
# ФОНОВЫЕ ЗАДАЧИ
# ==========================

async def periodic_expire_check():
    await asyncio.sleep(5)
    while True:
        now_dt = datetime.now()
        cursor.execute("SELECT * FROM subscriptions WHERE paid = 1")
        rows = cursor.fetchall()

        for user_id, paid, start_date, end_date, last_tx_amount, last_tx_time in rows:
            try:
                end_dt = datetime.strptime(end_date, "%Y-%m-%d %H:%M")
            except Exception:
                continue

            if end_dt < now_dt:
                cursor.execute(
                    "UPDATE subscriptions SET paid = 0 WHERE user_id = ?",
                    (user_id,),
                )
                conn.commit()
                try:
                    # выкидываем из канала и сразу разбаниваем, чтобы при новой оплате снова зайти
                    await bot.kick_chat_member(CHANNEL_ID, user_id)
                    await bot.unban_chat_member(CHANNEL_ID, user_id)
                except Exception:
                    pass

                try:
                    await bot.send_message(
                        user_id,
                        "⚠️ Твоя подписка истекла.\n"
                        "Если хочешь продолжать получать сигналы — просто оформи оплату снова.",
                    )
                except Exception:
                    pass

                await log_to_admin(f"Подписка пользователя {user_id} истекла.")
        await asyncio.sleep(EXPIRE_CHECK_INTERVAL)


async def periodic_auto_check_payments():
    await asyncio.sleep(10)
    while True:
        if pending_payments:
            for uid in list(pending_payments.keys()):
                try:
                    if await check_trx_payment(uid):
                        info = pending_payments.get(uid)
                        if not info:
                            continue
                        amount = info["amount"]
                        product = info["product"]

                        referrer_id = record_payment(uid, product, amount)

                        if product == PRODUCT_SUBSCRIPTION:
                            upsert_subscription_after_payment(uid, amount)
                            try:
                                invite = await bot.create_chat_invite_link(
                                    CHANNEL_ID, member_limit=1
                                )
                                await bot.send_message(
                                    uid,
                                    "✅ Оплата за подписку найдена автоматически!\n"
                                    f"Вот твоя ссылка в закрытый канал:\n{invite.invite_link}",
                                )
                            except Exception as e:
                                await bot.send_message(
                                    uid,
                                    "Оплата прошла, но не удалось автоматически создать ссылку.\n"
                                    "Напиши админу — он выдаст доступ вручную.",
                                )
                                await log_to_admin(f"AUTO-LINK ERROR {uid}: {e}")
                        elif product == PRODUCT_TRADING_COURSE:
                            await bot.send_message(
                                uid,
                                "✅ Оплата за <b>обучение трейдингу</b> найдена автоматически!\n"
                                "Админ свяжется с тобой и выдаст доступ к материалам."
                            )
                        elif product == PRODUCT_ARBITRAGE_COURSE:
                            await bot.send_message(
                                uid,
                                "✅ Оплата за <b>курс по арбитражу трафика</b> найдена автоматически!\n"
                                "Админ свяжется с тобой и выдаст доступ к обучению."
                            )

                        if referrer_id:
                            stats = calculate_partner_stats(referrer_id)
                            try:
                                await bot.send_message(
                                    referrer_id,
                                    "🔥 <b>Новый заработок по партнёрке (авто)!</b>\n\n"
                                    f"Кто-то оформил: <b>{PRODUCT_TITLES[product]}</b>\n"
                                    f"Сумма: <b>{amount:.2f} USDT</b>\n\n"
                                    f"Твой общий заработок: <b>{stats['total_earned']:.2f} USDT</b>\n"
                                    f"Текущий баланс: <b>{stats['balance']:.2f} USDT</b>\n"
                                )
                            except Exception:
                                pass

                        await log_to_admin(
                            f"AUTO PAYMENT: user {uid}, product {product}, amount {amount}"
                        )
                        pending_payments.pop(uid, None)

                except Exception as e:
                    logger.error(f"Ошибка в periodic_auto_check_payments: {e}")
        await asyncio.sleep(PAYMENT_SCAN_INTERVAL)


# ==========================
# ЗАПУСК
# ==========================

async def on_startup(dp: Dispatcher):
    asyncio.create_task(periodic_expire_check())
    asyncio.create_task(periodic_auto_check_payments())
    await log_to_admin("Бот TradeX Partner Bot успешно запущен ✅")


if __name__ == "__main__":
    from aiogram import executor
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)




