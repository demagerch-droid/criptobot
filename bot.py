# ===================== CONFIG =====================
BOT_TOKEN = "PASTE_YOUR_BOT_TOKEN"
ADMIN_ID = 123456789  # твой Telegram ID
PRICE = 200.0

PRIVATE_GROUP_LINK = "https://t.me/your_private_group"

REF_L1 = 0.50
REF_L2 = 0.10
DB_PATH = "database.db"
# ==================================================

import asyncio
import aiosqlite
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.storage.memory import MemoryStorage

# ===================== DB =====================
async def db():
    conn = await aiosqlite.connect(DB_PATH)
    await conn.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        ref1 INTEGER,
        ref2 INTEGER,
        access INTEGER DEFAULT 0,
        created TEXT
    );
    CREATE TABLE IF NOT EXISTS payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        status TEXT
    );
    CREATE TABLE IF NOT EXISTS wallets(
        user_id INTEGER PRIMARY KEY,
        balance REAL DEFAULT 0
    );
    """)
    await conn.commit()
    return conn

# ===================== KEYBOARDS =====================
def menu(access: bool):
    kb = []
    if access:
        kb += [
            [InlineKeyboardButton("📚 Обучение", callback_data="learn")],
            [InlineKeyboardButton("👤 Профиль", callback_data="profile")],
            [InlineKeyboardButton("🎁 Партнёрка", callback_data="partner")]
        ]
    else:
        kb += [
            [InlineKeyboardButton("💳 Купить доступ", callback_data="buy")],
            [InlineKeyboardButton("🎁 Партнёрка", callback_data="partner")]
        ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

# ===================== BOT =====================
bot = Bot(8491759417:AAFCnK5ubsubVQPYvdOTp6p0MRJrtA4m5p8)
dp = Dispatcher(storage=MemoryStorage())

@dp.message(CommandStart())
async def start(m: Message):
    payload = m.text.split(" ", 1)
    ref = None
    if len(payload) == 2 and payload[1].startswith("ref_"):
        ref = int(payload[1].replace("ref_", ""))

    async with await db() as d:
        user = await d.execute("SELECT * FROM users WHERE user_id=?", (m.from_user.id,))
        if not await user.fetchone():
            ref2 = None
            if ref:
                r = await d.execute("SELECT ref1 FROM users WHERE user_id=?", (ref,))
                row = await r.fetchone()
                if row:
                    ref2 = row[0]
            await d.execute(
                "INSERT INTO users VALUES (?,?,?,?,?)",
                (m.from_user.id, ref, ref2, 0, str(datetime.now()))
            )
            await d.execute("INSERT OR IGNORE INTO wallets VALUES (?,0)", (m.from_user.id,))
            await d.commit()

        acc = await d.execute("SELECT access FROM users WHERE user_id=?", (m.from_user.id,))
        access = (await acc.fetchone())[0]

    await m.answer(
        "🔥 <b>Арбитраж трафика TikTok</b>\n\n"
        "Обучение + партнёрка.\n"
        "Выбери действие:",
        reply_markup=menu(access),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "buy")
async def buy(c: CallbackQuery):
    await c.message.edit_text(
        f"💳 <b>Покупка доступа</b>\n\n"
        f"Цена: <b>${PRICE}</b>\n\n"
        "После оплаты нажми кнопку ниже.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton("✅ Я оплатил", callback_data="paid")]
        ]),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "paid")
async def paid(c: CallbackQuery):
    async with await db() as d:
        await d.execute(
            "INSERT INTO payments(user_id,status) VALUES (?,?)",
            (c.from_user.id, "pending")
        )
        await d.commit()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("✅ Подтвердить", callback_data=f"ap_{c.from_user.id}")],
        [InlineKeyboardButton("❌ Отклонить", callback_data=f"rej_{c.from_user.id}")]
    ])
    await bot.send_message(
        ADMIN_ID,
        f"💳 Заявка на оплату\nЮзер: {c.from_user.id}",
        reply_markup=kb
    )
    await c.message.edit_text("⏳ Заявка отправлена админу.")

@dp.callback_query(F.data.startswith("ap_"))
async def approve(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return
    uid = int(c.data.replace("ap_", ""))
    async with await db() as d:
        await d.execute("UPDATE users SET access=1 WHERE user_id=?", (uid,))
        await d.execute("UPDATE payments SET status='ok' WHERE user_id=?", (uid,))
        await d.commit()
    await bot.send_message(uid, "✅ Доступ активирован навсегда")
    await c.message.edit_text("✅ Подтверждено")

@dp.callback_query(F.data == "partner")
async def partner(c: CallbackQuery):
    link = f"https://t.me/{(await bot.get_me()).username}?start=ref_{c.from_user.id}"
    await c.message.edit_text(
        f"🎁 <b>Партнёрка</b>\n\n"
        f"1 уровень: 50%\n"
        f"2 уровень: 10%\n\n"
        f"🔗 Твоя ссылка:\n<code>{link}</code>",
        parse_mode="HTML"
    )

# ===================== RUN =====================
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
