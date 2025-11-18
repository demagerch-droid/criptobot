import asyncio
import logging
import random
import sqlite3
import csv
from datetime import datetime, timedelta

import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

# ==========================
# НАСТРОЙКИ
# ==========================

BOT_TOKEN = "ТОКЕН"
ADMIN_ID = 682938643

TRONGRID_API_KEY = "b33b8d65-10c9-4f7b-99e0-ab47f3bbb60f"
WALLET_ADDRESS = "TSY9xf24bQ3Kbd1Njp2w4pEEoqJow1nfpr"
CHANNEL_ID = -1003464806734   # закрытый канал

PRICE_USDT = 50
SUB_DAYS = 30

DB_PATH = "database.db"

EXPIRE_CHECK_INTERVAL = 1800
PAYMENT_SCAN_INTERVAL = 60

# ==========================
# ИНИЦИАЛИЗАЦИЯ
# ==========================

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS subscriptions(
        user_id INTEGER PRIMARY KEY,
        unique_price REAL,
        paid INTEGER,
        start_date TEXT,
        end_date TEXT,
        tx_amount REAL,
        tx_time TEXT
    );
    """
)

cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_seen TEXT,
        last_active TEXT
    );
    """
)

conn.commit()

user_unique_price: dict[int, float] = {}

# ==========================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================

def is_admin(message: types.Message) -> bool:
    return message.from_user.id == ADMIN_ID

def save_user(user_id: int, username: str | None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    cursor.execute(
        """
        INSERT INTO users (user_id, username, first_seen, last_active)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET last_active = excluded.last_active
        """,
        (user_id, username or "", now, now),
    )
    conn.commit()

def get_subscription(user_id: int):
    cursor.execute(
        "SELECT * FROM subscriptions WHERE user_id = ?",
        (user_id,),
    )
    return cursor.fetchone()

def save_payment(user_id: int, unique_price: float, tx_amount: float):
    now = datetime.now()
    end = now + timedelta(days=SUB_DAYS)
    cursor.execute(
        """
        INSERT OR REPLACE INTO subscriptions
        (user_id, unique_price, paid, start_date, end_date, tx_amount, tx_time)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            unique_price,
            1,
            now.strftime("%Y-%m-%d %H:%M"),
            end.strftime("%Y-%m-%d %H:%M"),
            tx_amount,
            now.strftime("%Y-%m-%d %H:%M"),
        ),
    )
    conn.commit()

def set_paid(user_id: int, paid: int):
    cursor.execute("UPDATE subscriptions SET paid = ? WHERE user_id = ?", (paid, user_id))
    conn.commit()

async def log_to_admin(text: str):
    try:
        await bot.send_message(ADMIN_ID, f"🛠 LOG:\n{text}")
    except:
        pass

# ==========================
# ПРОВЕРКА ОПЛАТ TRONGRID
# ==========================

async def check_trx_payment(user_id: int) -> bool:
    target_amount = user_unique_price.get(user_id)
    if target_amount is None:
        return False

    url = f"https://api.trongrid.io/v1/accounts/{WALLET_ADDRESS}/transactions/trc20"
    headers = {"TRON-PRO-API-KEY": TRONGRID_API_KEY}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()

    for tx in data.get("data", []):
        try:
            raw_value = tx.get("value") or tx.get("amount")
            if raw_value is None:
                continue
            amount = int(raw_value) / 1_000_000
            if abs(amount - target_amount) < 0.000001:
                return True
        except:
            continue

    return False


# ==========================
# КЛАВИАТУРЫ
# ==========================

def main_keyboard():
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        keyboard=[
            [KeyboardButton("📌 О боте"), KeyboardButton("📈 Получить сигналы")],
            [KeyboardButton("💰 Тарифы"), KeyboardButton("📞 Поддержка")],
            [KeyboardButton("👤 Профиль")],
        ],
    )

def admin_keyboard():
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        keyboard=[
            [KeyboardButton("👥 Все пользователи")],
            [KeyboardButton("📊 Все подписчики")],
            [KeyboardButton("🔥 Активные подписчики")],
            [KeyboardButton("⏳ Истёкшие")],
            [KeyboardButton("🧾 История платежей")],
            [KeyboardButton("📤 Экспорт CSV")],
        ],
    )

# ==========================
# ОБЫЧНЫЕ КОМАНДЫ
# ==========================

@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    save_user(message.from_user.id, message.from_user.username)

    row = get_subscription(message.from_user.id)
    now = datetime.now()

    if row:
        _, _, paid, _, end_date, _, _ = row
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d %H:%M")
        except:
            end_dt = now

        if paid == 1 and end_dt > now:
            await message.answer(
                f"🔥 У тебя есть активная подписка!\n"
                f"До: *{end_date}*",
                parse_mode="Markdown",
            )

    await message.answer(
        "👋 Добро пожаловать в *Crypto Signals Bot*!",
        reply_markup=main_keyboard(),
        parse_mode="Markdown"
    )


@dp.message_handler(lambda m: m.text == "📌 О боте")
async def about(message: types.Message):
    await message.answer(
        "🤖 *Crypto Signals Bot*\n\n"
        "📈 BTC/ETH/ALT сигналы\n"
        "💰 USDT(TRC20)\n",
        parse_mode="Markdown"
    )


@dp.message_handler(lambda m: m.text == "💰 Тарифы")
async def tariffs(message: types.Message):
    await message.answer(
        f"💰 1 месяц — {PRICE_USDT} USDT\n"
        f"💰 2 месяца — {PRICE_USDT+30} USDT",
        parse_mode="Markdown"
    )


@dp.message_handler(lambda m: m.text == "📞 Поддержка")
async def support(message: types.Message):
    await message.answer(
        "Пиши сюда: @your_support_username",
        parse_mode="Markdown"
    )

@dp.message_handler(lambda m: m.text == "👤 Профиль")
async def profile(message: types.Message):
    row = get_subscription(message.from_user.id)
    now = datetime.now()

    if not row:
        return await message.answer("Нет подписки. Купи через «📈 Получить сигналы»")

    user_id, unique_price, paid, start_date, end_date, tx_amount, tx_time = row

    try:
        end_dt = datetime.strptime(end_date, "%Y-%m-%d %H:%M")
    except:
        end_dt = now

    status = "🟢 Активна" if (paid == 1 and end_dt > now) else "🔴 Нет подписки"
    days_left = max((end_dt - now).days, 0)

    text = (
        f"👤 *Профиль*\n\n"
        f"Статус: {status}\n"
        f"До: {end_date}\n"
        f"Осталось дней: {days_left}\n"
        f"Оплата: {tx_amount} USDT\n"
        f"Когда: {tx_time}"
    )
    await message.answer(text, parse_mode="Markdown")


@dp.message_handler(lambda m: m.text == "📈 Получить сигналы")
async def buy(message: types.Message):
    unique_tail = random.randint(1, 999)
    unique_price = float(f"{PRICE_USDT}.{unique_tail:03d}")
    user_unique_price[message.from_user.id] = unique_price

    kb = ReplyKeyboardMarkup(
        resize_keyboard=True,
        keyboard=[
            [KeyboardButton("🔄 Проверить оплату")],
            [KeyboardButton("⬅️ В главное меню")],
        ],
    )

    await message.answer(
        f"Отправь *РОВНО* `{unique_price}` USDT(TRC20)\nНа адрес:\n`{WALLET_ADDRESS}`",
        reply_markup=kb,
        parse_mode="Markdown"
    )


@dp.message_handler(lambda m: m.text == "🔄 Проверить оплату")
async def check_payment(message: types.Message):
    await message.answer("⏳ Проверяю...")

    if await check_trx_payment(message.from_user.id):
        amount = user_unique_price.get(message.from_user.id)
        save_payment(message.from_user.id, amount, amount)
        user_unique_price.pop(message.from_user.id, None)

        try:
            invite = await bot.create_chat_invite_link(CHANNEL_ID, member_limit=1)
            await message.answer(f"✔ Оплата найдена!\nВход: {invite.invite_link}")
        except:
            await message.answer("Оплачено, но не могу создать ссылку!")

    else:
        await message.answer("❌ Платёж не найден.")

@dp.message_handler(commands=['admin'])
async def admin_panel(message: types.Message):
    if not is_admin(message): return
    await message.answer("Админ-панель:", reply_markup=admin_keyboard())


@dp.message_handler(lambda m: m.text == "📤 Экспорт CSV")
async def export_csv(message: types.Message):
    if not is_admin(message): return

    cursor.execute("SELECT * FROM subscriptions")
    rows = cursor.fetchall()

    if not rows:
        return await message.answer("Нет данных.")

    filename = "subscriptions_export.csv"
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["user_id", "unique_price", "paid", "start", "end", "amount", "time"])
        for row in rows:
            writer.writerow(row)

    with open(filename, "rb") as f:
        await message.answer_document(f, caption="Экспорт подписчиков")


# ==========================
# ФОНОВЫЕ ЗАДАЧИ
# ==========================

async def periodic_tasks():
    await asyncio.sleep(10)
    while True:
        for user_id in list(user_unique_price.keys()):
            if await check_trx_payment(user_id):
                amount = user_unique_price[user_id]
                save_payment(user_id, amount, amount)
                user_unique_price.pop(user_id, None)
        await asyncio.sleep(PAYMENT_SCAN_INTERVAL)


# ==========================
# ЗАПУСК БОТА
# ==========================

async def main():
    asyncio.create_task(periodic_tasks())
    await dp.start_polling()


if __name__ == "__main__":
    asyncio.run(main())

