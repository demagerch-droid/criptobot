# -*- coding: utf-8 -*-
"""
Traffic Partner Bot (УБД/перелив трафика) — AIogram 3 + aiosqlite
Логика:
- Постоянное нижнее меню (ReplyKeyboard): 🧠 Обучение / 💸 Заработок / 👤 Профиль
- Оплата полного доступа (USDT TRC20) через TronGrid с "хвостиком" суммы
- Доступ "навсегда" (без подписки)
- 8 модулей обучения: структура видна всем, но модули "закрыты" до оплаты
- Партнёрка 2 уровня: 50% (1 линия) + 10% (2 линия) от базовой цены (без хвостика)
- Админ-панель доступна только ADMIN_ID
"""

import asyncio
import logging
import random
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN

import aiohttp
import aiosqlite
from aiogram.client.default import DefaultBotProperties
from aiogram import Bot, Dispatcher, Router, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

# ---------------------------------------------------------------------------
# НАСТРОЙКИ (всё в коде — как ты просил) 
# ---------------------------------------------------------------------------

BOT_TOKEN = "8491759417:AAFCnK5ubsubVQPYvdOTp6p0MRJrtA4m5p8"  # ⚠️ лучше вставить новый токен (перевыпусти в BotFather)
ADMIN_ID = 8585550939  # твой TG ID (числом)

# TronGrid / TRC20 (USDT)
TRONGRID_API_KEY = "PASTE_TRONGRID_KEY_HERE"
WALLET_ADDRESS = "PASTE_YOUR_TRON_WALLET_HERE"  # адрес получателя USDT TRC20 (T...)
USDT_TRON_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"  # стандартный контракт USDT TRC20

# Цена доступа
PRICE_ACCESS = Decimal("200")  # $200
LEVEL1_PERCENT = Decimal("0.50")  # 50%
LEVEL2_PERCENT = Decimal("0.10")  # 10%

# Куда вести после оплаты
PRIVATE_CHANNEL_URL = "https://t.me/your_private_channel_or_invite_link"
COMMUNITY_GROUP_URL = "https://t.me/your_group_or_forum_link"
SUPPORT_CONTACT = "@your_support_username"

DB_PATH = "database.db"

# Антиспам (сек)
ANTISPAM_SECONDS = 1.2

# ---------------------------------------------------------------------------
# ОФОРМЛЕНИЕ / ТЕКСТЫ / МОДУЛИ
# ---------------------------------------------------------------------------

PROJECT_NAME = "Traffic Partner Bot"
ACCESS_NAME = "Полный доступ"

MODULES = [
    "1️⃣ Модуль 1 — Старт: система перелива (УБД) и воронка",
    "2️⃣ Модуль 2 — TikTok / Reels: как добывать трафик стабильно",
    "3️⃣ Модуль 3 — Прогрев без канала: сценарии и структура контента",
    "4️⃣ Модуль 4 — Связка «ролик → бот → покупка»",
    "5️⃣ Модуль 5 — Аналитика, трекинг, метрики, оптимизация",
    "6️⃣ Модуль 6 — Мультиаккаунты, антидетект, прокси, безопасность",
    "7️⃣ Модуль 7 — Масштабирование и команда (делегирование)",
    "8️⃣ Модуль 8 — Партнёрка: как строить сеть и удержание",
]

# Контент модулей — ты потом заменишь текст внутри
MODULE_TEXT_PLACEHOLDER = (
    "📝 <b>Здесь будет текст модуля</b>\n\n"
    "Ты можешь вставить сюда свой контент, чек-листы, ссылки, примеры связок и т.д.\n"
    "Чтобы было красиво — делай:\n"
    "• короткие блоки\n"
    "• списки\n"
    "• выделение жирным\n\n"
    "Если хочешь — я помогу красиво оформить каждый модуль."
)

# ---------------------------------------------------------------------------
# ЛОГИ
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("traffic_bot")

# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

@asynccontextmanager
async def get_db():
    db = await aiosqlite.connect(DB_PATH, timeout=30)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL;")
    await db.execute("PRAGMA foreign_keys=ON;")
    await db.execute("PRAGMA busy_timeout=30000;")
    try:
        yield db
    finally:
        await db.close()


async def init_db():
    async with get_db() as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER UNIQUE NOT NULL,
                username TEXT DEFAULT '',
                first_name TEXT DEFAULT '',
                referrer_id INTEGER,
                reg_date TEXT,
                full_access INTEGER DEFAULT 0,
                balance TEXT DEFAULT '0',
                total_earned TEXT DEFAULT '0',
                is_blocked INTEGER DEFAULT 0,
                FOREIGN KEY(referrer_id) REFERENCES users(id)
            );
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                product_code TEXT NOT NULL,         -- "access"
                amount TEXT NOT NULL,               -- Decimal as string (важно!)
                status TEXT NOT NULL,               -- "pending" / "paid"
                created_at TEXT NOT NULL,
                paid_at TEXT,
                tx_id TEXT,
                UNIQUE(tx_id),
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS progress (
                user_id INTEGER PRIMARY KEY,
                module_index INTEGER DEFAULT -1,
                updated_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            """
        )
        await db.commit()


async def get_user_by_tg(tg_id: int):
    async with get_db() as db:
        cur = await db.execute(
            """
            SELECT id, tg_id, username, first_name, referrer_id, reg_date, full_access, balance, total_earned
            FROM users WHERE tg_id = ?
            """,
            (tg_id,),
        )
        return await cur.fetchone()


async def create_user(tg_id: int, username: str, first_name: str, referrer_id: int | None):
    reg_date = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO users (tg_id, username, first_name, referrer_id, reg_date)
            VALUES (?, ?, ?, ?, ?)
            """,
            (tg_id, username or "", first_name or "", referrer_id, reg_date),
        )
        await db.commit()
        cur = await db.execute("SELECT id FROM users WHERE tg_id = ?", (tg_id,))
        row = await cur.fetchone()
        return row["id"]


async def update_user_profile(tg_id: int, username: str, first_name: str):
    async with get_db() as db:
        await db.execute(
            "UPDATE users SET username = ?, first_name = ? WHERE tg_id = ?",
            (username or "", first_name or "", tg_id),
        )
        await db.commit()


async def get_or_create_user(tg_user, referrer_tg_id: int | None):
    existing = await get_user_by_tg(tg_user.id)
    if existing:
        await update_user_profile(tg_user.id, tg_user.username or "", tg_user.first_name or "")
        return existing["id"]

    referrer_id = None
    if referrer_tg_id and referrer_tg_id != tg_user.id:
        ref_row = await get_user_by_tg(referrer_tg_id)
        if ref_row:
            referrer_id = ref_row["id"]

    return await create_user(
        tg_id=tg_user.id,
        username=tg_user.username or "",
        first_name=tg_user.first_name or "",
        referrer_id=referrer_id,
    )


async def set_full_access(user_db_id: int, value: bool = True):
    async with get_db() as db:
        await db.execute("UPDATE users SET full_access = ? WHERE id = ?", (1 if value else 0, user_db_id))
        await db.commit()


async def has_access_by_tg(tg_id: int) -> bool:
    row = await get_user_by_tg(tg_id)
    return bool(row and row["full_access"])


async def get_referrer_chain(user_db_id: int):
    async with get_db() as db:
        cur = await db.execute("SELECT referrer_id FROM users WHERE id = ?", (user_db_id,))
        r1 = await cur.fetchone()
        lvl1 = r1["referrer_id"] if r1 and r1["referrer_id"] else None

        lvl2 = None
        if lvl1:
            cur2 = await db.execute("SELECT referrer_id FROM users WHERE id = ?", (lvl1,))
            r2 = await cur2.fetchone()
            lvl2 = r2["referrer_id"] if r2 and r2["referrer_id"] else None

        return lvl1, lvl2


async def add_balance(user_db_id: int, amount: Decimal):
    async with get_db() as db:
        cur = await db.execute("SELECT balance, total_earned FROM users WHERE id = ?", (user_db_id,))
        row = await cur.fetchone()
        bal = Decimal(row["balance"])
        tot = Decimal(row["total_earned"])
        bal += amount
        tot += amount
        await db.execute(
            "UPDATE users SET balance = ?, total_earned = ? WHERE id = ?",
            (str(bal.quantize(Decimal("0.01"))), str(tot.quantize(Decimal("0.01"))), user_db_id),
        )
        await db.commit()


async def count_referrals(user_db_id: int):
    async with get_db() as db:
        cur1 = await db.execute("SELECT COUNT(*) AS c FROM users WHERE referrer_id = ?", (user_db_id,))
        lvl1 = (await cur1.fetchone())["c"]

        cur2 = await db.execute(
            """
            SELECT COUNT(*) AS c FROM users
            WHERE referrer_id IN (SELECT id FROM users WHERE referrer_id = ?)
            """,
            (user_db_id,),
        )
        lvl2 = (await cur2.fetchone())["c"]

        return int(lvl1), int(lvl2)


async def top_referrers(limit: int = 10):
    async with get_db() as db:
        cur = await db.execute(
            """
            SELECT u.username, u.first_name, COUNT(r.id) AS cnt
            FROM users u
            LEFT JOIN users r ON r.referrer_id = u.id
            GROUP BY u.id
            HAVING cnt > 0
            ORDER BY cnt DESC
            LIMIT ?
            """,
            (limit,),
        )
        return await cur.fetchall()


async def save_progress(user_db_id: int, module_index: int):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO progress (user_id, module_index, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET module_index = excluded.module_index, updated_at = excluded.updated_at
            """,
            (user_db_id, module_index, now),
        )
        await db.commit()


async def get_progress(user_db_id: int) -> int:
    async with get_db() as db:
        cur = await db.execute("SELECT module_index FROM progress WHERE user_id = ?", (user_db_id,))
        row = await cur.fetchone()
        return int(row["module_index"]) if row else -1


# ---------------------------------------------------------------------------
# PURCHASES / PAYMENTS
# ---------------------------------------------------------------------------

def _make_unique_amount(base: Decimal) -> Decimal:
    tail = Decimal(random.randint(1, 999)) / Decimal("1000")  # 0.001 ... 0.999
    return (base + tail).quantize(Decimal("0.000"), rounding=ROUND_DOWN)


async def create_purchase(user_db_id: int, product_code: str, base_price: Decimal) -> int:
    amount = _make_unique_amount(base_price)
    created_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO purchases (user_id, product_code, amount, status, created_at)
            VALUES (?, ?, ?, 'pending', ?)
            """,
            (user_db_id, product_code, str(amount), created_at),
        )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid() AS id")
        row = await cur.fetchone()
        return int(row["id"])


async def get_purchase(purchase_id: int):
    async with get_db() as db:
        cur = await db.execute(
            """
            SELECT id, user_id, product_code, amount, status, created_at, paid_at, tx_id
            FROM purchases WHERE id = ?
            """,
            (purchase_id,),
        )
        return await cur.fetchone()


async def mark_purchase_paid(purchase_id: int, tx_id: str):
    paid_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    async with get_db() as db:
        await db.execute(
            """
            UPDATE purchases SET status='paid', paid_at=?, tx_id=? WHERE id=?
            """,
            (paid_at, tx_id, purchase_id),
        )
        await db.commit()


async def get_tg_id_by_user_db(user_db_id: int) -> int | None:
    async with get_db() as db:
        cur = await db.execute("SELECT tg_id FROM users WHERE id = ?", (user_db_id,))
        row = await cur.fetchone()
        return int(row["tg_id"]) if row else None


async def process_successful_payment(bot: Bot, purchase_row):
    """
    Начисляет доступ и партнёрку (50% + 10%) с базовой цены (без хвоста).
    """
    purchase_id = int(purchase_row["id"])
    user_db_id = int(purchase_row["user_id"])
    product_code = purchase_row["product_code"]

    if product_code != "access":
        return

    # 1) открыть доступ
    await set_full_access(user_db_id, True)

    # 2) партнёрка
    lvl1, lvl2 = await get_referrer_chain(user_db_id)
    base = PRICE_ACCESS

    lvl1_bonus = (base * LEVEL1_PERCENT).quantize(Decimal("0.01"))
    lvl2_bonus = (base * LEVEL2_PERCENT).quantize(Decimal("0.01"))

    if lvl1:
        await add_balance(lvl1, lvl1_bonus)
        tg_id_1 = await get_tg_id_by_user_db(lvl1)
        if tg_id_1:
            try:
                await bot.send_message(
                    tg_id_1,
                    f"💰 <b>Начисление партнёрки</b>\n\n"
                    f"Твой партнёр оплатил доступ.\n"
                    f"Тебе начислено: <b>{lvl1_bonus}$</b> (1 уровень).",
                    reply_markup=main_kb(),
                )
            except Exception:
                pass

    if lvl2:
        await add_balance(lvl2, lvl2_bonus)
        tg_id_2 = await get_tg_id_by_user_db(lvl2)
        if tg_id_2:
            try:
                await bot.send_message(
                    tg_id_2,
                    f"💸 <b>Начисление партнёрки</b>\n\n"
                    f"Покупка прошла во 2-й линии.\n"
                    f"Тебе начислено: <b>{lvl2_bonus}$</b> (2 уровень).",
                    reply_markup=main_kb(),
                )
            except Exception:
                pass

    # 3) уведомить покупателя
    buyer_tg_id = await get_tg_id_by_user_db(user_db_id)
    if buyer_tg_id:
        await bot.send_message(
            buyer_tg_id,
            "✅ <b>Оплата подтверждена!</b>\n\n"
            f"Доступ открыт <b>навсегда</b>.\n\n"
            "Теперь тебе доступны:\n"
            "• все 8 модулей обучения\n"
            "• партнёрская программа 50% + 10%\n"
            "• реферальная ссылка и статистика\n\n"
            "Жми <b>«Обучение»</b> снизу — и начинай 🔥",
            reply_markup=main_kb(),
        )


async def fetch_trc20_transactions() -> list:
    """
    TronGrid: последние TRC20 транзакции по нашему кошельку
    """
    url = f"https://api.trongrid.io/v1/accounts/{WALLET_ADDRESS}/transactions/trc20"
    headers = {"TRON-PRO-API-KEY": TRONGRID_API_KEY} if TRONGRID_API_KEY else {}
    params = {
        "limit": 50,
        "contract_address": USDT_TRON_CONTRACT,
        "only_confirmed": "true",
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url, params=params, timeout=25) as resp:
            if resp.status != 200:
                logger.error("TronGrid error: %s %s", resp.status, await resp.text())
                return []
            data = await resp.json()
            return data.get("data", [])


async def find_payment_for_amount(amount: Decimal, created_at: datetime) -> str | None:
    """
    Ищем транзакцию по точной сумме (с хвостом) и по времени создания заявки.
    """
    txs = await fetch_trc20_transactions()
    if not txs:
        return None

    for tx in txs:
        try:
            to_addr = tx.get("to")
            if to_addr != WALLET_ADDRESS:
                continue

            token_info = tx.get("token_info") or {}
            decimals = int(token_info.get("decimals", 6))

            raw_value = Decimal(tx.get("value", "0"))
            value = raw_value / (Decimal(10) ** decimals)

            # допускаем минимальную погрешность
            if abs(value - amount) > Decimal("0.0005"):
                continue

            ts_ms = tx.get("block_timestamp")
            tx_time = datetime.utcfromtimestamp(ts_ms / 1000.0)

            # транзакция не должна быть сильно раньше заявки (например, старше 24 часов)
            if tx_time + timedelta(hours=24) < created_at:
                continue

            tx_id = tx.get("transaction_id")
            return tx_id
        except Exception:
            continue

    return None


# ---------------------------------------------------------------------------
# UI: клавиатуры
# ---------------------------------------------------------------------------

def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🧠 Обучение"),
                KeyboardButton(text="💸 Заработок"),
                KeyboardButton(text="👤 Профиль"),
            ]
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери раздел снизу 👇",
    )


def kb_back(cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=cb)]])


def kb_buy(back_cb: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"💳 Купить доступ ({PRICE_ACCESS}$)", callback_data="buy_access")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)],
        ]
    )
    return kb


def kb_training(has_access: bool) -> InlineKeyboardMarkup:
    rows = []
    for idx, title in enumerate(MODULES):
        if has_access:
            rows.append([InlineKeyboardButton(text=title, callback_data=f"mod:{idx}")])
        else:
            # структура видна, но модуль закрыт
            rows.append([InlineKeyboardButton(text=f"🔒 {title}", callback_data=f"locked:{idx}")])

    bottom = []
    if has_access:
        bottom.append([InlineKeyboardButton(text="🔗 Перейти в закрытый канал", url=PRIVATE_CHANNEL_URL)])
        bottom.append([InlineKeyboardButton(text="💬 Перейти в группу (чат/форум)", url=COMMUNITY_GROUP_URL)])
    else:
        bottom.append([InlineKeyboardButton(text=f"💳 Купить доступ ({PRICE_ACCESS}$)", callback_data="buy_access")])

    rows.extend(bottom)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_earn(has_access: bool) -> InlineKeyboardMarkup:
    if not has_access:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📌 Как работает партнёрка", callback_data="earn_info")],
                [InlineKeyboardButton(text=f"💳 Открыть доступ ({PRICE_ACCESS}$)", callback_data="buy_access")],
            ]
        )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Моя реферальная ссылка", callback_data="my_ref")],
            [InlineKeyboardButton(text="📊 Моя статистика", callback_data="my_stats")],
            [InlineKeyboardButton(text="🏆 Топ рефералов", callback_data="top_refs")],
            [InlineKeyboardButton(text="💸 Запросить вывод", callback_data="withdraw")],
        ]
    )


def kb_profile(has_access: bool, is_admin: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_access:
        rows.append([InlineKeyboardButton(text="🔗 Моя реферальная ссылка", callback_data="my_ref")])
        rows.append([InlineKeyboardButton(text="📊 Моя статистика", callback_data="my_stats")])
    else:
        rows.append([InlineKeyboardButton(text=f"💳 Купить доступ ({PRICE_ACCESS}$)", callback_data="buy_access")])

    rows.append([InlineKeyboardButton(text="ℹ️ FAQ", callback_data="faq")])
    rows.append([InlineKeyboardButton(text="💬 Поддержка", callback_data="support")])

    if is_admin:
        rows.append([InlineKeyboardButton(text="🔐 Админ-панель", callback_data="admin_panel")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_payment(purchase_id: int, back_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"check_pay:{purchase_id}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)],
        ]
    )


# ---------------------------------------------------------------------------
# Антиспам
# ---------------------------------------------------------------------------

_user_last_action: dict[int, datetime] = {}

def is_spam(user_id: int) -> bool:
    now = datetime.utcnow()
    last = _user_last_action.get(user_id)
    _user_last_action[user_id] = now
    if not last:
        return False
    return (now - last).total_seconds() < ANTISPAM_SECONDS


# ---------------------------------------------------------------------------
# Bot / Router
# ---------------------------------------------------------------------------

router = Router()
BOT_USERNAME_CACHE: str | None = None


def is_admin(tg_id: int) -> bool:
    return tg_id == ADMIN_ID


# ---------------------------------------------------------------------------
# Основные экраны
# ---------------------------------------------------------------------------

async def show_home(message: Message):
    text = (
        f"👋 <b>Привет!</b> Ты в <b>{PROJECT_NAME}</b>\n\n"
        "Здесь всё построено максимально просто:\n"
        "1) Ты изучаешь систему перелива трафика (УБД) по модулям\n"
        "2) Забираешь готовую механику воронки «контент → бот → покупка»\n"
        "3) При желании подключаешь партнёрку и зарабатываешь на рекомендациях\n\n"
        f"🎟 <b>{ACCESS_NAME}</b> — <b>{PRICE_ACCESS}$</b> и <b>навсегда</b>.\n"
        "После оплаты:\n"
        "• открываются все 8 модулей\n"
        "• появляется реферальная ссылка\n"
        "• включается статистика и партнёрские начисления\n\n"
        "👇 Выбирай раздел снизу: <b>Обучение / Заработок / Профиль</b>"
    )
    await message.answer(text, reply_markup=main_kb())


async def show_training(target: Message | CallbackQuery, edit: bool = False):
    if isinstance(target, CallbackQuery):
        tg_id = target.from_user.id
        msg = target.message
    else:
        tg_id = target.from_user.id
        msg = target

    has = await has_access_by_tg(tg_id)

    text = (
        "🧠 <b>Обучение</b>\n\n"
        "Ниже — структура из <b>8 модулей</b>.\n"
        "✅ Структура видна всем.\n"
        "🔒 Открыть каждый модуль можно после оплаты.\n\n"
        "⚡️ Рекомендация: проходи по порядку — это система.\n"
    )

    if not has:
        text += (
            "\n<b>Чтобы открыть доступ:</b>\n"
            f"• оплата: <b>{PRICE_ACCESS}$</b> (USDT TRC20)\n"
            "• доступ навсегда\n"
            "• партнёрка 50% + 10% включается сразу после оплаты\n"
        )
    else:
        text += (
            "\n✅ <b>Доступ открыт</b>\n"
            "Теперь ты можешь открывать модули + перейти в закрытый канал и группу."
        )

    kb = kb_training(has)

    if edit and isinstance(target, CallbackQuery):
        try:
            await msg.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass

    await msg.answer(text, reply_markup=kb)


async def show_earn(target: Message | CallbackQuery, edit: bool = False):
    if isinstance(target, CallbackQuery):
        tg_id = target.from_user.id
        msg = target.message
    else:
        tg_id = target.from_user.id
        msg = target

    has = await has_access_by_tg(tg_id)

    if not has:
        text = (
            "💸 <b>Заработок</b>\n\n"
            "У нас простая партнёрка на 2 уровня:\n"
            f"• <b>50%</b> с 1-й линии\n"
            f"• <b>10%</b> со 2-й линии\n\n"
            "⚠️ Но реферальная ссылка и статистика открываются только после покупки полного доступа.\n\n"
            "Хочешь зарабатывать — открой доступ и получи свою реф-ссылку."
        )
    else:
        text = (
            "💸 <b>Заработок</b>\n\n"
            "Тут всё по делу:\n"
            "• твоя реферальная ссылка\n"
            "• статистика и баланс\n"
            "• топ партнёров\n"
            "• запрос вывода администратору\n\n"
            "Выбирай действие 👇"
        )

    kb = kb_earn(has)

    if edit and isinstance(target, CallbackQuery):
        try:
            await msg.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass

    await msg.answer(text, reply_markup=kb)


async def show_profile(target: Message | CallbackQuery, edit: bool = False):
    if isinstance(target, CallbackQuery):
        tg_id = target.from_user.id
        msg = target.message
    else:
        tg_id = target.from_user.id
        msg = target

    row = await get_user_by_tg(tg_id)
    if not row:
        # на всякий случай
        await get_or_create_user(target.from_user if isinstance(target, Message) else target.from_user, None)
        row = await get_user_by_tg(tg_id)

    user_db_id = int(row["id"])
    username = row["username"] or ""
    first_name = row["first_name"] or ""
    reg_date = row["reg_date"] or "—"
    access = bool(row["full_access"])
    balance = Decimal(row["balance"])
    total_earned = Decimal(row["total_earned"])
    lvl1, lvl2 = await count_referrals(user_db_id)
    progress = await get_progress(user_db_id)
    progress_str = f"{max(progress+1, 0)}/{len(MODULES)}" if progress >= 0 else f"0/{len(MODULES)}"

    text = (
        "👤 <b>Профиль</b>\n\n"
        f"👋 Имя: <b>{first_name or '—'}</b>\n"
        f"🔹 Username: @{username if username else '—'}\n"
        f"🆔 ID: <code>{tg_id}</code>\n"
        f"📅 Регистрация: <b>{reg_date}</b>\n\n"
        f"🎟 Доступ: <b>{'Открыт ✅' if access else 'Не оплачен ❌'}</b>\n"
        f"📚 Прогресс обучения: <b>{progress_str}</b>\n\n"
        "🤝 <b>Партнёрка</b>\n"
        f"• 1 линия: <b>{lvl1}</b>\n"
        f"• 2 линия: <b>{lvl2}</b>\n\n"
        f"💰 Баланс к выводу: <b>{balance.quantize(Decimal('0.01'))}$</b>\n"
        f"🏦 Всего заработано: <b>{total_earned.quantize(Decimal('0.01'))}$</b>"
    )

    kb = kb_profile(access, is_admin(tg_id))

    if edit and isinstance(target, CallbackQuery):
        try:
            await msg.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass

    await msg.answer(text, reply_markup=kb)


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message):
    if is_spam(message.from_user.id):
        return

    args = (message.text or "").split(maxsplit=1)
    ref_tg_id = None
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            ref_tg_id = int(args[1].split("_", 1)[1])
        except Exception:
            ref_tg_id = None

    await get_or_create_user(message.from_user, ref_tg_id)
    await show_home(message)


# ---------------------------------------------------------------------------
# Нижнее меню (ReplyKeyboard)
# ---------------------------------------------------------------------------

@router.message(F.text == "🧠 Обучение")
async def menu_training(message: Message):
    if is_spam(message.from_user.id):
        return
    await show_training(message)


@router.message(F.text == "💸 Заработок")
async def menu_earn(message: Message):
    if is_spam(message.from_user.id):
        return
    await show_earn(message)


@router.message(F.text == "👤 Профиль")
async def menu_profile(message: Message):
    if is_spam(message.from_user.id):
        return
    await show_profile(message)


# ---------------------------------------------------------------------------
# Обучение: модули (lock/open)
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("locked:"))
async def cb_locked_module(call: CallbackQuery):
    await call.answer("🔒 Модуль закрыт. Сначала оплати доступ.", show_alert=True)


@router.callback_query(F.data.startswith("mod:"))
async def cb_open_module(call: CallbackQuery):
    tg_id = call.from_user.id
    if not await has_access_by_tg(tg_id):
        await call.answer("🔒 Сначала оплати доступ.", show_alert=True)
        return

    idx = int(call.data.split(":", 1)[1])
    idx = max(0, min(idx, len(MODULES) - 1))

    user = await get_user_by_tg(tg_id)
    if user:
        await save_progress(int(user["id"]), idx)

    title = MODULES[idx]
    text = f"🧠 <b>{title}</b>\n\n{MODULE_TEXT_PLACEHOLDER}"
    try:
        await call.message.edit_text(text, reply_markup=kb_training(True))
    except Exception:
        await call.message.answer(text, reply_markup=kb_training(True))

    await call.answer()


# ---------------------------------------------------------------------------
# Заработок: инфо / реф / статистика / топ
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "earn_info")
async def cb_earn_info(call: CallbackQuery):
    text = (
        "📌 <b>Как работает партнёрка</b>\n\n"
        f"✅ После оплаты доступа (<b>{PRICE_ACCESS}$</b>) ты получаешь:\n"
        "• личную реферальную ссылку\n"
        "• статистику по партнёрам\n"
        "• начисления на баланс\n\n"
        "💰 Начисления:\n"
        f"• <b>50%</b> с 1-й линии\n"
        f"• <b>10%</b> со 2-й линии\n\n"
        "⚠️ Начисления идут только с покупки полного доступа."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Открыть доступ", callback_data="buy_access")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:earn")],
        ]
    )
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "my_ref")
async def cb_my_ref(call: CallbackQuery):
    if not await has_access_by_tg(call.from_user.id):
        text = (
            "🔗 <b>Реферальная ссылка</b>\n\n"
            "Сначала открой полный доступ — и здесь появится твоя реф-ссылка + статистика."
        )
        try:
            await call.message.edit_text(text, reply_markup=kb_buy("back:profile"))
        except Exception:
            await call.message.answer(text, reply_markup=kb_buy("back:profile"))
        await call.answer()
        return

    global BOT_USERNAME_CACHE
    if not BOT_USERNAME_CACHE:
        me = await call.bot.get_me()
        BOT_USERNAME_CACHE = me.username

    ref_link = f"https://t.me/{BOT_USERNAME_CACHE}?start=ref_{call.from_user.id}"

    text = (
        "🔗 <b>Твоя реферальная ссылка</b>\n\n"
        f"<code>{ref_link}</code>\n\n"
        "Как использовать:\n"
        "• вставляй ссылку в TikTok/Instagram/YouTube Shorts\n"
        "• веди трафик сразу в бота (без прогрев-канала)\n"
        "• получай начисления с оплат партнёров\n\n"
        "⚠️ Не спамь. Делай нормальный контент и связки — так будет конверт."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Моя статистика", callback_data="my_stats")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:earn")],
        ]
    )
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "my_stats")
async def cb_my_stats(call: CallbackQuery):
    row = await get_user_by_tg(call.from_user.id)
    if not row:
        await call.answer("Сначала нажми /start", show_alert=True)
        return

    user_db_id = int(row["id"])
    lvl1, lvl2 = await count_referrals(user_db_id)
    balance = Decimal(row["balance"])
    total_earned = Decimal(row["total_earned"])
    access = bool(row["full_access"])

    text = (
        "📊 <b>Моя статистика</b>\n\n"
        f"Доступ: <b>{'Открыт ✅' if access else 'Не оплачен ❌'}</b>\n\n"
        f"👥 Партнёры 1 линии: <b>{lvl1}</b>\n"
        f"👥 Партнёры 2 линии: <b>{lvl2}</b>\n"
        f"👥 Всего: <b>{lvl1 + lvl2}</b>\n\n"
        f"💰 Баланс к выводу: <b>{balance.quantize(Decimal('0.01'))}$</b>\n"
        f"🏦 Всего заработано: <b>{total_earned.quantize(Decimal('0.01'))}$</b>"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Моя реферальная ссылка", callback_data="my_ref")],
            [InlineKeyboardButton(text="🏆 Топ рефералов", callback_data="top_refs")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:earn")],
        ]
    )
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "top_refs")
async def cb_top_refs(call: CallbackQuery):
    rows = await top_referrers(10)
    if not rows:
        text = "🏆 <b>Топ рефералов</b>\n\nПока тут пусто. Стань первым 😄"
    else:
        lines = ["🏆 <b>Топ рефералов (1 линия)</b>\n"]
        for i, r in enumerate(rows, start=1):
            name = f"@{r['username']}" if r["username"] else (r["first_name"] or "Без имени")
            lines.append(f"{i}. {name} — <b>{r['cnt']}</b>")
        text = "\n".join(lines)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Моя статистика", callback_data="my_stats")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:earn")],
        ]
    )
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "withdraw")
async def cb_withdraw(call: CallbackQuery):
    row = await get_user_by_tg(call.from_user.id)
    if not row:
        await call.answer("Сначала нажми /start", show_alert=True)
        return

    if not bool(row["full_access"]):
        await call.answer("Сначала открой доступ — партнёрка активируется после оплаты.", show_alert=True)
        return

    balance = Decimal(row["balance"])
    text = (
        "💸 <b>Запрос вывода</b>\n\n"
        f"Твой текущий баланс: <b>{balance.quantize(Decimal('0.01'))}$</b>\n\n"
        "Чтобы запросить вывод, напиши администратору в поддержку и укажи:\n"
        "• сумму\n"
        "• твой USDT-адрес (TRC20)\n"
        "• скрин/ID профиля (если нужно)\n\n"
        f"Поддержка: {SUPPORT_CONTACT}"
    )
    try:
        await call.message.edit_text(text, reply_markup=kb_back("back:earn"))
    except Exception:
        await call.message.answer(text, reply_markup=kb_back("back:earn"))

    # можно тихо уведомить админа (не обязательно)
    try:
        await call.bot.send_message(
            ADMIN_ID,
            f"📥 <b>Запрос вывода</b>\n"
            f"От: <code>{call.from_user.id}</code>\n"
            f"Username: @{call.from_user.username or '—'}\n"
            f"Баланс: {balance}$",
        )
    except Exception:
        pass

    await call.answer()


# ---------------------------------------------------------------------------
# Profile: FAQ / Support
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "faq")
async def cb_faq(call: CallbackQuery):
    text = (
        "ℹ️ <b>FAQ</b>\n\n"
        f"❓ <b>Что входит в доступ за {PRICE_ACCESS}$?</b>\n"
        "• 8 модулей обучения по переливу трафика (УБД)\n"
        "• доступ к закрытому каналу/материалам\n"
        "• партнёрка 50% + 10%\n"
        "• реферальная ссылка + статистика\n\n"
        "❓ <b>Доступ навсегда?</b>\n"
        "Да. Оплата один раз.\n\n"
        "❓ <b>Что если оплатил, а доступ не открылся?</b>\n"
        "Нажми «Проверить оплату». Если сеть задержала транзакцию — подожди пару минут.\n"
        f"Если всё равно нет — напиши в поддержку: {SUPPORT_CONTACT}\n\n"
        "⚠️ <b>Важно</b>\n"
        "Результат зависит от твоих действий. Бот — это инструмент, а не «волшебная кнопка»."
    )
    try:
        await call.message.edit_text(text, reply_markup=kb_back("back:profile"))
    except Exception:
        await call.message.answer(text, reply_markup=kb_back("back:profile"))
    await call.answer()


@router.callback_query(F.data == "support")
async def cb_support(call: CallbackQuery):
    text = (
        "💬 <b>Поддержка</b>\n\n"
        f"Пиши сюда: {SUPPORT_CONTACT}\n\n"
        "Если вопрос по оплате — сразу отправь:\n"
        "• сумму\n"
        "• время оплаты\n"
        "• tx hash (если есть)\n"
    )
    try:
        await call.message.edit_text(text, reply_markup=kb_back("back:profile"))
    except Exception:
        await call.message.answer(text, reply_markup=kb_back("back:profile"))
    await call.answer()


# ---------------------------------------------------------------------------
# Покупка доступа / Проверка оплаты
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "buy_access")
async def cb_buy_access(call: CallbackQuery):
    # если уже есть доступ — не показываем оплату
    if await has_access_by_tg(call.from_user.id):
        await call.answer("✅ У тебя уже открыт доступ.", show_alert=True)
        return

    user_row = await get_user_by_tg(call.from_user.id)
    if not user_row:
        await get_or_create_user(call.from_user, None)
        user_row = await get_user_by_tg(call.from_user.id)

    user_db_id = int(user_row["id"])
    purchase_id = await create_purchase(user_db_id, "access", PRICE_ACCESS)
    purchase = await get_purchase(purchase_id)
    amount = Decimal(purchase["amount"])

    text = (
        f"💳 <b>Оплата доступа ({PRICE_ACCESS}$)</b>\n\n"
        "Оплата в <b>USDT (TRC20)</b>.\n\n"
        f"Кошелёк для оплаты:\n<code>{WALLET_ADDRESS}</code>\n\n"
        f"Сумма к оплате: <b>{amount} USDT</b>\n\n"
        "⚠️ Важно: отправь <b>ТОЧНО</b> эту сумму (с хвостиком), иначе бот не сопоставит платёж.\n\n"
        "После оплаты нажми «Проверить оплату»."
    )

    # куда возвращаться: обучение (чаще всего человек там)
    kb = kb_payment(purchase_id, back_cb="back:training")

    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)

    await call.answer()


@router.callback_query(F.data.startswith("check_pay:"))
async def cb_check_pay(call: CallbackQuery):
    try:
        purchase_id = int(call.data.split(":", 1)[1])
    except Exception:
        await call.answer("Некорректный ID оплаты.", show_alert=True)
        return

    purchase = await get_purchase(purchase_id)
    if not purchase:
        await call.answer("Оплата не найдена. Попробуй заново.", show_alert=True)
        return

    # безопасность: проверяем что это покупка этого пользователя
    user_row = await get_user_by_tg(call.from_user.id)
    if not user_row or int(user_row["id"]) != int(purchase["user_id"]):
        await call.answer("Эта оплата не принадлежит тебе.", show_alert=True)
        return

    if purchase["status"] == "paid":
        await call.answer("Уже подтверждено ✅", show_alert=True)
        return

    amount = Decimal(purchase["amount"])
    created_at = datetime.strptime(purchase["created_at"], "%Y-%m-%d %H:%M:%S")

    await call.answer("🔎 Проверяю транзакции в Tron...")

    tx_id = await find_payment_for_amount(amount, created_at)
    if not tx_id:
        text = (
            "❌ <b>Платёж пока не найден</b>\n\n"
            "Проверь:\n"
            "• ты отправил <b>точно</b> указанную сумму\n"
            "• ты отправил на <b>правильный</b> адрес\n"
            "• прошло ли 1–3 минуты (иногда сеть задерживает)\n\n"
            "Если всё ок, просто попробуй ещё раз через минуту."
        )
        try:
            await call.message.edit_text(text, reply_markup=kb_payment(purchase_id, "back:training"))
        except Exception:
            await call.message.answer(text, reply_markup=kb_payment(purchase_id, "back:training"))
        return

    await mark_purchase_paid(purchase_id, tx_id)
    purchase2 = await get_purchase(purchase_id)
    await process_successful_payment(call.bot, purchase2)

    # после оплаты — показываем обучение (уже открыто)
    try:
        await show_training(call, edit=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Back navigation callbacks
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("back:"))
async def cb_back(call: CallbackQuery):
    where = call.data.split(":", 1)[1]
    if where == "training":
        await show_training(call, edit=True)
    elif where == "earn":
        await show_earn(call, edit=True)
    elif where == "profile":
        await show_profile(call, edit=True)
    else:
        await show_home(call.message)
    await call.answer()


# ---------------------------------------------------------------------------
# Admin panel
# ---------------------------------------------------------------------------

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        return

    text = (
        "🔐 <b>Админ-панель</b>\n\n"
        "Команды:\n"
        "• <code>/grant 123456789</code> — выдать доступ по TG ID\n"
        "• <code>/grant @username</code> — выдать доступ по username\n"
        "• <code>/user 123456789</code> — инфо по пользователю\n"
        "• <code>/stats</code> — общая статистика\n\n"
        "Также есть кнопки ниже 👇"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👥 Пользователи (последние 20)", callback_data="admin_users")],
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        ]
    )
    await message.answer(text, reply_markup=kb)


async def _find_user_by_identifier(identifier: str):
    identifier = identifier.strip()
    async with get_db() as db:
        if identifier.startswith("@"):
            username = identifier[1:]
            cur = await db.execute("SELECT * FROM users WHERE username = ?", (username,))
            return await cur.fetchone()
        else:
            try:
                tg_id = int(identifier)
            except Exception:
                return None
            cur = await db.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
            return await cur.fetchone()


@router.message(Command("grant"))
async def cmd_grant(message: Message):
    if not is_admin(message.from_user.id):
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: <code>/grant 123456789</code> или <code>/grant @username</code>")
        return

    ident = parts[1].strip()
    user = await _find_user_by_identifier(ident)
    if not user:
        await message.answer("Пользователь не найден в базе. Пусть сначала нажмёт /start.")
        return

    await set_full_access(int(user["id"]), True)
    await message.answer("✅ Доступ выдан.")
    try:
        await message.bot.send_message(
            int(user["tg_id"]),
            "🎟 <b>Тебе выдан доступ администратором.</b>\n\n"
            "Теперь все модули открыты + партнёрка активна.",
            reply_markup=main_kb(),
        )
    except Exception:
        pass


@router.message(Command("user"))
async def cmd_user(message: Message):
    if not is_admin(message.from_user.id):
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: <code>/user 123456789</code> или <code>/user @username</code>")
        return

    user = await _find_user_by_identifier(parts[1])
    if not user:
        await message.answer("Пользователь не найден.")
        return

    user_db_id = int(user["id"])
    lvl1, lvl2 = await count_referrals(user_db_id)

    text = (
        "👤 <b>Пользователь</b>\n\n"
        f"TG ID: <code>{user['tg_id']}</code>\n"
        f"Username: @{user['username'] or '—'}\n"
        f"Имя: {user['first_name'] or '—'}\n"
        f"Регистрация: {user['reg_date'] or '—'}\n\n"
        f"Доступ: {'да ✅' if user['full_access'] else 'нет ❌'}\n"
        f"Рефы: 1л={lvl1}, 2л={lvl2}\n"
        f"Баланс: {user['balance']}$\n"
        f"Всего заработано: {user['total_earned']}$"
    )
    await message.answer(text)


async def build_admin_stats_text() -> str:
    async with get_db() as db:
        cur_u = await db.execute("SELECT COUNT(*) AS c FROM users")
        users_cnt = (await cur_u.fetchone())["c"]

        cur_p = await db.execute("SELECT COUNT(*) AS c FROM purchases WHERE status='paid'")
        paid_cnt = (await cur_p.fetchone())["c"]

        cur_rev = await db.execute("SELECT amount FROM purchases WHERE status='paid' AND product_code='access'")
        rows = await cur_rev.fetchall()
        revenue = sum([Decimal(r["amount"]) for r in rows], Decimal("0"))

    text = (
        "📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: <b>{users_cnt}</b>\n"
        f"✅ Оплаченных доступов: <b>{paid_cnt}</b>\n"
        f"💵 Сумма оплат (с хвостами): <b>{revenue.quantize(Decimal('0.001'))} USDT</b>\n"
    )
    return text


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message.from_user.id):
        return

    text = await build_admin_stats_text()
    await message.answer(text)


@router.callback_query(F.data == "admin_users")
async def cb_admin_users(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return

    async with get_db() as db:
        cur = await db.execute(
            "SELECT tg_id, username, first_name, reg_date, full_access FROM users ORDER BY id DESC LIMIT 20"
        )
        rows = await cur.fetchall()

    lines = ["👥 <b>Последние 20 пользователей</b>\n"]
    for r in rows:
        name = f"@{r['username']}" if r["username"] else (r["first_name"] or "—")
        lines.append(f"• {name} — <code>{r['tg_id']}</code> — {'✅' if r['full_access'] else '❌'}")
    text = "\n".join(lines)

    try:
        await call.message.edit_text(text, reply_markup=kb_back("back:profile"))
    except Exception:
        await call.message.answer(text, reply_markup=kb_back("back:profile"))
    await call.answer()


@router.callback_query(F.data == "admin_stats")
async def cb_admin_stats(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return

    text = await build_admin_stats_text()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👥 Пользователи (последние 20)", callback_data="admin_users")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:profile")],
        ]
    )

    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)

    await call.answer()


@router.callback_query(F.data == "admin_panel")
async def cb_admin_panel(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await call.message.answer("Открой /admin — там команды и кнопки.", reply_markup=main_kb())
    await call.answer()


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------

@router.message()
async def fallback(message: Message):
    if is_spam(message.from_user.id):
        return
    await message.answer(
        "🤔 Я тебя не понял.\n\n"
        "Используй кнопки снизу: <b>Обучение</b>, <b>Заработок</b>, <b>Профиль</b>.\n"
        "Или нажми /start, чтобы перезагрузить меню.",
        reply_markup=main_kb(),
    )


# ---------------------------------------------------------------------------
# START
# ---------------------------------------------------------------------------

async def main():
    bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
    dp = Dispatcher()
    dp.include_router(router)

    await init_db()

    me = await bot.get_me()
    global BOT_USERNAME_CACHE
    BOT_USERNAME_CACHE = me.username
    logger.info("Bot started as @%s", BOT_USERNAME_CACHE)

    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
