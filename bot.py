# -*- coding: utf-8 -*-
"""
Traffic Partner Bot — Aiogram 3 + SQLite (aiosqlite)

ВАЖНО ПРО БАЗУ (честно и без магии):
- Если на Railway НЕТ Volume (persistent storage), то SQLite-файл будет жить в контейнере и
  может исчезать при redeploy/перезапуске. Это не "баг", это отсутствие диска.
- Чтобы база НЕ пропадала: подключи Volume и смонтируй его в /data.
- Этот файл использует DB_PATH из переменной окружения (если задана),
  иначе /data/database.db (если /data существует), иначе рядом с bot.py.

Мини-диагностика:
- Команда /db (только для ADMIN_ID) покажет путь к БД, размер и список таблиц.
"""

import asyncio
import logging
import random
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN

import aiohttp
import aiosqlite

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

# ---------------------------------------------------------------------------
# НАСТРОЙКИ (Railway Variables)
# ---------------------------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")
USDT_TRON_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

PRICE_ACCESS = Decimal(os.getenv("PRICE_ACCESS", "200"))
LEVEL1_PERCENT = Decimal(os.getenv("LEVEL1_PERCENT", "0.50"))
LEVEL2_PERCENT = Decimal(os.getenv("LEVEL2_PERCENT", "0.10"))

PRIVATE_CHANNEL_URL = os.getenv("PRIVATE_CHANNEL_URL", "https://t.me/your_private_channel_or_invite_link")
COMMUNITY_GROUP_URL = os.getenv("COMMUNITY_GROUP_URL", "https://t.me/your_group_or_forum_link")
SUPPORT_CONTACT = os.getenv("SUPPORT_CONTACT", "@your_support_username")

ANTISPAM_SECONDS = float(os.getenv("ANTISPAM_SECONDS", "1.2"))

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

MODULE_TEXT_PLACEHOLDER = (
    "📝 <b>Здесь будет текст модуля</b>\n\n"
    "Вставь сюда свой контент: чек-листы, ссылки, примеры, скрины.\n"
    "Совет по оформлению:\n"
    "• короткие блоки\n"
    "• списки\n"
    "• выделение жирным\n"
)

# ---------------------------------------------------------------------------
# ЛОГИ
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("traffic_bot")

# ---------------------------------------------------------------------------
# DB PATH
# ---------------------------------------------------------------------------

_env_db_path = os.getenv("DB_PATH", "").strip()
if _env_db_path:
    DB_PATH = _env_db_path
else:
    DB_DIR = "/data" if os.path.isdir("/data") else os.path.dirname(os.path.abspath(__file__))
    os.makedirs(DB_DIR, exist_ok=True)
    DB_PATH = os.path.join(DB_DIR, "database.db")

print("DB_PATH =", DB_PATH)

# ---------------------------------------------------------------------------
# DB HELPERS
# ---------------------------------------------------------------------------

@asynccontextmanager
async def get_db():
    db = await aiosqlite.connect(DB_PATH, timeout=30)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON;")
    await db.execute("PRAGMA busy_timeout=30000;")
    # WAL обычно ок. Если увидишь "database is locked" — поменяй на DELETE.
    await db.execute("PRAGMA journal_mode=WAL;")
    try:
        yield db
    finally:
        await db.close()


async def init_db():
    """
    Создаёт таблицы. Если таблица users старая (без id) — мигрирует.
    """
    async with get_db() as db:
        cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
        users_exists = await cur.fetchone()

        async def create_users_table():
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

        if not users_exists:
            await create_users_table()
        else:
            cur = await db.execute("PRAGMA table_info(users)")
            cols = [r["name"] for r in await cur.fetchall()]
            if "id" not in cols:
                logger.warning("DB MIGRATION: old users table without 'id'. Migrating...")

                await db.execute("PRAGMA foreign_keys=OFF;")
                await db.execute("ALTER TABLE users RENAME TO users_old;")
                await create_users_table()

                cur = await db.execute("PRAGMA table_info(users_old)")
                old_cols = {r["name"] for r in await cur.fetchall()}

                tg_col = next((c for c in ("tg_id", "telegram_id", "user_id") if c in old_cols), None)
                ref_col = next((c for c in ("referrer_id", "referrer_tg_id", "ref_tg_id") if c in old_cols), None)

                def expr(col, default_sql):
                    return f"COALESCE({col}, {default_sql})" if col in old_cols else default_sql

                if tg_col:
                    await db.execute(f"""
                        INSERT INTO users (tg_id, username, first_name, referrer_id, reg_date, full_access, balance, total_earned, is_blocked)
                        SELECT
                            {expr(tg_col, "0")},
                            {expr("username", "''")},
                            {expr("first_name", "''")},
                            NULL,
                            {expr("reg_date", "''")},
                            {expr("full_access", "0")},
                            {expr("balance", "'0'")},
                            {expr("total_earned", "'0'")},
                            {expr("is_blocked", "0")}
                        FROM users_old;
                    """)
                    if ref_col:
                        await db.execute(f"""
                            UPDATE users
                            SET referrer_id = (
                                SELECT u2.id FROM users u2
                                WHERE u2.tg_id = (
                                    SELECT o.{ref_col} FROM users_old o WHERE o.{tg_col} = users.tg_id
                                )
                            )
                            WHERE (
                                SELECT o.{ref_col} FROM users_old o WHERE o.{tg_col} = users.tg_id
                            ) IS NOT NULL;
                        """)
                await db.execute("DROP TABLE users_old;")
                await db.execute("PRAGMA foreign_keys=ON;")
                await db.commit()

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                product_code TEXT NOT NULL,
                amount TEXT NOT NULL,
                status TEXT NOT NULL,
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

# ---------------------------------------------------------------------------
# USERS
# ---------------------------------------------------------------------------

async def get_user_by_tg(tg_id: int):
    async with get_db() as db:
        cur = await db.execute(
            "SELECT id, tg_id, username, first_name, referrer_id, reg_date, full_access, balance, total_earned FROM users WHERE tg_id=?",
            (tg_id,),
        )
        return await cur.fetchone()


async def create_user(tg_id: int, username: str, first_name: str, referrer_id: int | None):
    reg_date = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    async with get_db() as db:
        await db.execute(
            "INSERT INTO users (tg_id, username, first_name, referrer_id, reg_date) VALUES (?, ?, ?, ?, ?)",
            (tg_id, username or "", first_name or "", referrer_id, reg_date),
        )
        await db.commit()
        cur = await db.execute("SELECT id FROM users WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        return int(row["id"])


async def update_user_profile(tg_id: int, username: str, first_name: str):
    async with get_db() as db:
        await db.execute("UPDATE users SET username=?, first_name=? WHERE tg_id=?", (username or "", first_name or "", tg_id))
        await db.commit()


async def get_or_create_user(tg_user, referrer_tg_id: int | None):
    existing = await get_user_by_tg(tg_user.id)
    if existing:
        await update_user_profile(tg_user.id, tg_user.username or "", tg_user.first_name or "")
        return int(existing["id"])

    referrer_id = None
    if referrer_tg_id and referrer_tg_id != tg_user.id:
        ref_row = await get_user_by_tg(referrer_tg_id)
        if ref_row:
            referrer_id = int(ref_row["id"])

    return await create_user(tg_user.id, tg_user.username or "", tg_user.first_name or "", referrer_id)


async def set_full_access(user_db_id: int, value: bool = True):
    async with get_db() as db:
        await db.execute("UPDATE users SET full_access=? WHERE id=?", (1 if value else 0, user_db_id))
        await db.commit()


async def has_access_by_tg(tg_id: int) -> bool:
    row = await get_user_by_tg(tg_id)
    return bool(row and row["full_access"])


async def get_referrer_chain(user_db_id: int):
    async with get_db() as db:
        cur = await db.execute("SELECT referrer_id FROM users WHERE id=?", (user_db_id,))
        r1 = await cur.fetchone()
        lvl1 = int(r1["referrer_id"]) if r1 and r1["referrer_id"] else None

        lvl2 = None
        if lvl1:
            cur2 = await db.execute("SELECT referrer_id FROM users WHERE id=?", (lvl1,))
            r2 = await cur2.fetchone()
            lvl2 = int(r2["referrer_id"]) if r2 and r2["referrer_id"] else None

        return lvl1, lvl2


async def add_balance(user_db_id: int, amount: Decimal):
    async with get_db() as db:
        cur = await db.execute("SELECT balance, total_earned FROM users WHERE id=?", (user_db_id,))
        row = await cur.fetchone()
        bal = Decimal(row["balance"])
        tot = Decimal(row["total_earned"])
        bal += amount
        tot += amount
        await db.execute(
            "UPDATE users SET balance=?, total_earned=? WHERE id=?",
            (str(bal.quantize(Decimal("0.01"))), str(tot.quantize(Decimal("0.01"))), user_db_id),
        )
        await db.commit()


async def count_referrals(user_db_id: int):
    async with get_db() as db:
        cur1 = await db.execute("SELECT COUNT(*) AS c FROM users WHERE referrer_id=?", (user_db_id,))
        lvl1 = int((await cur1.fetchone())["c"])

        cur2 = await db.execute(
            "SELECT COUNT(*) AS c FROM users WHERE referrer_id IN (SELECT id FROM users WHERE referrer_id=?)",
            (user_db_id,),
        )
        lvl2 = int((await cur2.fetchone())["c"])
        return lvl1, lvl2


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
            ON CONFLICT(user_id) DO UPDATE SET module_index=excluded.module_index, updated_at=excluded.updated_at
            """,
            (user_db_id, module_index, now),
        )
        await db.commit()


async def get_progress(user_db_id: int) -> int:
    async with get_db() as db:
        cur = await db.execute("SELECT module_index FROM progress WHERE user_id=?", (user_db_id,))
        row = await cur.fetchone()
        return int(row["module_index"]) if row else -1

# ---------------------------------------------------------------------------
# PURCHASES / PAYMENTS (оставлено как было: проверка по сумме)
# ---------------------------------------------------------------------------

def _make_unique_amount(base: Decimal) -> Decimal:
    tail = Decimal(random.randint(1, 999)) / Decimal("1000")
    return (base + tail).quantize(Decimal("0.000"), rounding=ROUND_DOWN)


async def create_purchase(user_db_id: int, product_code: str, base_price: Decimal) -> int:
    amount = _make_unique_amount(base_price)
    created_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    async with get_db() as db:
        await db.execute(
            "INSERT INTO purchases (user_id, product_code, amount, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
            (user_db_id, product_code, str(amount), created_at),
        )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid() AS id")
        row = await cur.fetchone()
        return int(row["id"])


async def get_purchase(purchase_id: int):
    async with get_db() as db:
        cur = await db.execute(
            "SELECT id, user_id, product_code, amount, status, created_at, paid_at, tx_id FROM purchases WHERE id=?",
            (purchase_id,),
        )
        return await cur.fetchone()


async def mark_purchase_paid(purchase_id: int, tx_id: str):
    paid_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    async with get_db() as db:
        await db.execute("UPDATE purchases SET status='paid', paid_at=?, tx_id=? WHERE id=?", (paid_at, tx_id, purchase_id))
        await db.commit()


async def get_tg_id_by_user_db(user_db_id: int) -> int | None:
    async with get_db() as db:
        cur = await db.execute("SELECT tg_id FROM users WHERE id=?", (user_db_id,))
        row = await cur.fetchone()
        return int(row["tg_id"]) if row else None


async def process_successful_payment(bot: Bot, purchase_row):
    if purchase_row["product_code"] != "access":
        return

    user_db_id = int(purchase_row["user_id"])
    await set_full_access(user_db_id, True)

    lvl1, lvl2 = await get_referrer_chain(user_db_id)
    base = PRICE_ACCESS

    lvl1_bonus = (base * LEVEL1_PERCENT).quantize(Decimal("0.01"))
    lvl2_bonus = (base * LEVEL2_PERCENT).quantize(Decimal("0.01"))

    if lvl1:
        await add_balance(lvl1, lvl1_bonus)
    if lvl2:
        await add_balance(lvl2, lvl2_bonus)

    buyer_tg_id = await get_tg_id_by_user_db(user_db_id)
    if buyer_tg_id:
        await bot.send_message(
            buyer_tg_id,
            "✅ <b>Оплата подтверждена!</b>\n\nДоступ открыт <b>навсегда</b>.\n\nЖми <b>«Обучение»</b> снизу.",
            reply_markup=main_kb(),
        )


async def fetch_trc20_transactions() -> list:
    if not WALLET_ADDRESS:
        logger.error("WALLET_ADDRESS пустой. Задай WALLET_ADDRESS в Railway Variables.")
        return []

    url = f"https://api.trongrid.io/v1/accounts/{WALLET_ADDRESS}/transactions/trc20"
    headers = {"TRON-PRO-API-KEY": TRONGRID_API_KEY} if TRONGRID_API_KEY else {}
    params = {"limit": 50, "contract_address": USDT_TRON_CONTRACT, "only_confirmed": "true"}

    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url, params=params, timeout=25) as resp:
            if resp.status != 200:
                logger.error("TronGrid error: %s %s", resp.status, await resp.text())
                return []
            data = await resp.json()
            return data.get("data", [])


async def find_payment_for_amount(amount: Decimal, created_at: datetime) -> str | None:
    txs = await fetch_trc20_transactions()
    if not txs:
        return None

    for tx in txs:
        try:
            if tx.get("to") != WALLET_ADDRESS:
                continue

            token_info = tx.get("token_info") or {}
            decimals = int(token_info.get("decimals", 6))
            raw_value = Decimal(tx.get("value", "0"))
            value = raw_value / (Decimal(10) ** decimals)

            if abs(value - amount) > Decimal("0.0005"):
                continue

            ts_ms = tx.get("block_timestamp")
            tx_time = datetime.utcfromtimestamp(ts_ms / 1000.0)

            if tx_time + timedelta(hours=24) < created_at:
                continue

            return tx.get("transaction_id")
        except Exception:
            continue

    return None

# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🧠 Обучение"), KeyboardButton(text="💸 Заработок"), KeyboardButton(text="👤 Профиль")]],
        resize_keyboard=True,
        input_field_placeholder="Выбери раздел снизу 👇",
    )


def kb_back(cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=cb)]])


def kb_buy(back_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"💳 Купить доступ ({PRICE_ACCESS}$)", callback_data="buy_access")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)],
        ]
    )


def kb_training(has_access: bool) -> InlineKeyboardMarkup:
    rows = []
    for idx, title in enumerate(MODULES):
        rows.append([InlineKeyboardButton(text=(title if has_access else f"🔒 {title}"), callback_data=(f"mod:{idx}" if has_access else f"locked:{idx}"))])

    if has_access:
        rows.append([InlineKeyboardButton(text="🔗 Перейти в закрытый канал", url=PRIVATE_CHANNEL_URL)])
        rows.append([InlineKeyboardButton(text="💬 Перейти в группу", url=COMMUNITY_GROUP_URL)])
    else:
        rows.append([InlineKeyboardButton(text=f"💳 Купить доступ ({PRICE_ACCESS}$)", callback_data="buy_access")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_earn(has_access: bool) -> InlineKeyboardMarkup:
    if not has_access:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📌 Как работает партнёрка", callback_data="earn_info")],
            [InlineKeyboardButton(text=f"💳 Открыть доступ ({PRICE_ACCESS}$)", callback_data="buy_access")],
        ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Моя реферальная ссылка", callback_data="my_ref")],
        [InlineKeyboardButton(text="📊 Моя статистика", callback_data="my_stats")],
        [InlineKeyboardButton(text="🏆 Топ рефералов", callback_data="top_refs")],
        [InlineKeyboardButton(text="💸 Запросить вывод", callback_data="withdraw")],
    ])


def kb_profile(has_access: bool, is_admin_flag: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_access:
        rows.append([InlineKeyboardButton(text="🔗 Моя реферальная ссылка", callback_data="my_ref")])
        rows.append([InlineKeyboardButton(text="📊 Моя статистика", callback_data="my_stats")])
    else:
        rows.append([InlineKeyboardButton(text=f"💳 Купить доступ ({PRICE_ACCESS}$)", callback_data="buy_access")])

    rows.append([InlineKeyboardButton(text="ℹ️ FAQ", callback_data="faq")])
    rows.append([InlineKeyboardButton(text="💬 Поддержка", callback_data="support")])

    if is_admin_flag:
        rows.append([InlineKeyboardButton(text="🔐 Админ-панель", callback_data="admin_panel")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_payment(purchase_id: int, back_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"check_pay:{purchase_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)],
    ])

# ---------------------------------------------------------------------------
# Антиспам
# ---------------------------------------------------------------------------

_user_last_action: dict[int, datetime] = {}

def is_spam(user_id: int) -> bool:
    now = datetime.utcnow()
    last = _user_last_action.get(user_id)
    _user_last_action[user_id] = now
    return bool(last and (now - last).total_seconds() < ANTISPAM_SECONDS)

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = Router()
BOT_USERNAME_CACHE: str | None = None

def is_admin(tg_id: int) -> bool:
    return tg_id == ADMIN_ID and ADMIN_ID != 0

# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------

async def show_home(message: Message):
    text = (
        f"👋 <b>Привет!</b> Ты в <b>{PROJECT_NAME}</b>\n\n"
        "1) Изучаешь систему перелива трафика (УБД)\n"
        "2) Забираешь механику «контент → бот → покупка»\n"
        "3) Подключаешь партнёрку и зарабатываешь\n\n"
        f"🎟 <b>{ACCESS_NAME}</b> — <b>{PRICE_ACCESS}$</b> и <b>навсегда</b>.\n\n"
        "👇 Выбирай раздел снизу"
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
        "🔒 Модули открываются после оплаты.\n"
    )
    if not has:
        text += f"\n\nЧтобы открыть доступ: <b>{PRICE_ACCESS}$</b> (USDT TRC20) — навсегда."
    else:
        text += "\n\n✅ <b>Доступ открыт</b>"

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
            "Партнёрка 2 уровня:\n"
            "• <b>50%</b> с 1-й линии\n"
            "• <b>10%</b> со 2-й линии\n\n"
            "Реф-ссылка и статистика — после покупки доступа."
        )
    else:
        text = "💸 <b>Заработок</b>\n\nВыбирай действие 👇"

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
        user_obj = target.from_user
    else:
        tg_id = target.from_user.id
        msg = target
        user_obj = target.from_user

    row = await get_user_by_tg(tg_id)
    if not row:
        await get_or_create_user(user_obj, None)
        row = await get_user_by_tg(tg_id)

    user_db_id = int(row["id"])
    access = bool(row["full_access"])
    balance = Decimal(row["balance"])
    total_earned = Decimal(row["total_earned"])
    lvl1, lvl2 = await count_referrals(user_db_id)
    progress = await get_progress(user_db_id)
    progress_str = f"{max(progress+1, 0)}/{len(MODULES)}" if progress >= 0 else f"0/{len(MODULES)}"

    text = (
        "👤 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{tg_id}</code>\n"
        f"🎟 Доступ: <b>{'Открыт ✅' if access else 'Не оплачен ❌'}</b>\n"
        f"📚 Прогресс: <b>{progress_str}</b>\n\n"
        "🤝 <b>Партнёрка</b>\n"
        f"• 1 линия: <b>{lvl1}</b>\n"
        f"• 2 линия: <b>{lvl2}</b>\n\n"
        f"💰 Баланс: <b>{balance.quantize(Decimal('0.01'))}$</b>\n"
        f"🏦 Всего: <b>{total_earned.quantize(Decimal('0.01'))}$</b>"
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
# /start + menu
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
# Training callbacks
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("locked:"))
async def cb_locked_module(call: CallbackQuery):
    await call.answer("🔒 Модуль закрыт. Сначала оплати доступ.", show_alert=True)

@router.callback_query(F.data.startswith("mod:"))
async def cb_open_module(call: CallbackQuery):
    if not await has_access_by_tg(call.from_user.id):
        await call.answer("🔒 Сначала оплати доступ.", show_alert=True)
        return

    idx = int(call.data.split(":", 1)[1])
    idx = max(0, min(idx, len(MODULES) - 1))

    user = await get_user_by_tg(call.from_user.id)
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
# Earn callbacks
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "earn_info")
async def cb_earn_info(call: CallbackQuery):
    text = (
        "📌 <b>Как работает партнёрка</b>\n\n"
        f"После оплаты доступа (<b>{PRICE_ACCESS}$</b>) у тебя появятся реф-ссылка, статистика и начисления.\n\n"
        "Начисления:\n"
        "• <b>50%</b> с 1-й линии\n"
        "• <b>10%</b> со 2-й линии"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Открыть доступ", callback_data="buy_access")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:earn")],
    ])
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data == "my_ref")
async def cb_my_ref(call: CallbackQuery):
    if not await has_access_by_tg(call.from_user.id):
        text = "🔗 <b>Реферальная ссылка</b>\n\nСначала открой полный доступ — и здесь появится твоя реф-ссылка."
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
    text = f"🔗 <b>Твоя реферальная ссылка</b>\n\n<code>{ref_link}</code>"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:earn")],
    ])
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
        f"Доступ: <b>{'Открыт ✅' if access else 'Не оплачен ❌'}</b>\n"
        f"👥 1 линия: <b>{lvl1}</b>\n"
        f"👥 2 линия: <b>{lvl2}</b>\n"
        f"💰 Баланс: <b>{balance.quantize(Decimal('0.01'))}$</b>\n"
        f"🏦 Всего: <b>{total_earned.quantize(Decimal('0.01'))}$</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:earn")],
    ])
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data == "top_refs")
async def cb_top_refs(call: CallbackQuery):
    rows = await top_referrers(10)
    if not rows:
        text = "🏆 <b>Топ рефералов</b>\n\nПока пусто."
    else:
        lines = ["🏆 <b>Топ рефералов</b>\n"]
        for i, r in enumerate(rows, start=1):
            name = f"@{r['username']}" if r["username"] else (r["first_name"] or "Без имени")
            lines.append(f"{i}. {name} — <b>{r['cnt']}</b>")
        text = "\n".join(lines)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:earn")],
    ])
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
        await call.answer("Сначала открой доступ.", show_alert=True)
        return

    balance = Decimal(row["balance"])
    text = (
        "💸 <b>Запрос вывода</b>\n\n"
        f"Баланс: <b>{balance.quantize(Decimal('0.01'))}$</b>\n\n"
        f"Напиши в поддержку: {SUPPORT_CONTACT}"
    )
    try:
        await call.message.edit_text(text, reply_markup=kb_back("back:earn"))
    except Exception:
        await call.message.answer(text, reply_markup=kb_back("back:earn"))

    if ADMIN_ID:
        try:
            await call.bot.send_message(ADMIN_ID, f"📥 Запрос вывода от <code>{call.from_user.id}</code>, баланс {balance}$")
        except Exception:
            pass
    await call.answer()

# ---------------------------------------------------------------------------
# FAQ / Support
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "faq")
async def cb_faq(call: CallbackQuery):
    text = (
        "ℹ️ <b>FAQ</b>\n\n"
        f"Доступ: <b>{PRICE_ACCESS}$</b> — навсегда.\n"
        "Если оплатил, а доступ не открылся — нажми «Проверить оплату».\n"
        f"Поддержка: {SUPPORT_CONTACT}"
    )
    try:
        await call.message.edit_text(text, reply_markup=kb_back("back:profile"))
    except Exception:
        await call.message.answer(text, reply_markup=kb_back("back:profile"))
    await call.answer()

@router.callback_query(F.data == "support")
async def cb_support(call: CallbackQuery):
    text = f"💬 <b>Поддержка</b>\n\nПиши сюда: {SUPPORT_CONTACT}"
    try:
        await call.message.edit_text(text, reply_markup=kb_back("back:profile"))
    except Exception:
        await call.message.answer(text, reply_markup=kb_back("back:profile"))
    await call.answer()

# ---------------------------------------------------------------------------
# Buy / Check pay
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "buy_access")
async def cb_buy_access(call: CallbackQuery):
    if await has_access_by_tg(call.from_user.id):
        await call.answer("✅ У тебя уже открыт доступ.", show_alert=True)
        return

    user_row = await get_user_by_tg(call.from_user.id)
    if not user_row:
        await get_or_create_user(call.from_user, None)
        user_row = await get_user_by_tg(call.from_user.id)

    purchase_id = await create_purchase(int(user_row["id"]), "access", PRICE_ACCESS)
    purchase = await get_purchase(purchase_id)
    amount = Decimal(purchase["amount"])

    text = (
        f"💳 <b>Оплата доступа ({PRICE_ACCESS}$)</b>\n\n"
        "Оплата: <b>USDT (TRC20)</b>\n\n"
        f"Адрес:\n<code>{WALLET_ADDRESS or '— не задан —'}</code>\n\n"
        f"Сумма: <b>{amount} USDT</b>\n\n"
        "⚠️ Отправь <b>точно</b> эту сумму.\n"
        "После оплаты нажми «Проверить оплату»."
    )
    try:
        await call.message.edit_text(text, reply_markup=kb_payment(purchase_id, "back:training"))
    except Exception:
        await call.message.answer(text, reply_markup=kb_payment(purchase_id, "back:training"))
    await call.answer()

@router.callback_query(F.data.startswith("check_pay:"))
async def cb_check_pay(call: CallbackQuery):
    purchase_id = int(call.data.split(":", 1)[1])
    purchase = await get_purchase(purchase_id)
    if not purchase:
        await call.answer("Оплата не найдена.", show_alert=True)
        return

    if purchase["status"] == "paid":
        await call.answer("Уже подтверждено ✅", show_alert=True)
        return

    amount = Decimal(purchase["amount"])
    created_at = datetime.strptime(purchase["created_at"], "%Y-%m-%d %H:%M:%S")

    await call.answer("🔎 Проверяю транзакции...")
    tx_id = await find_payment_for_amount(amount, created_at)
    if not tx_id:
        text = "❌ Платёж пока не найден. Подожди 1–3 минуты и проверь ещё раз."
        try:
            await call.message.edit_text(text, reply_markup=kb_payment(purchase_id, "back:training"))
        except Exception:
            await call.message.answer(text, reply_markup=kb_payment(purchase_id, "back:training"))
        return

    await mark_purchase_paid(purchase_id, tx_id)
    purchase2 = await get_purchase(purchase_id)
    await process_successful_payment(call.bot, purchase2)
    await show_training(call, edit=True)

# ---------------------------------------------------------------------------
# Back navigation
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
# ADMIN PANEL
# ---------------------------------------------------------------------------

def kb_admin_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика (users/purchases)", callback_data="admin_stats")],
        [InlineKeyboardButton(text="✅ Выдать доступ (инструкция)", callback_data="admin_grant_help")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:profile")],
    ])

@router.callback_query(F.data == "admin_panel")
async def cb_admin_panel(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа.", show_alert=True)
        return
    text = "🔐 <b>Админ-панель</b>\n\nВыбирай действие:"
    try:
        await call.message.edit_text(text, reply_markup=kb_admin_panel())
    except Exception:
        await call.message.answer(text, reply_markup=kb_admin_panel())
    await call.answer()

@router.callback_query(F.data == "admin_stats")
async def cb_admin_stats(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа.", show_alert=True)
        return
    async with get_db() as db:
        u = await (await db.execute("SELECT COUNT(*) AS c FROM users")).fetchone()
        p = await (await db.execute("SELECT COUNT(*) AS c FROM purchases")).fetchone()
        paid = await (await db.execute("SELECT COUNT(*) AS c FROM purchases WHERE status='paid'")).fetchone()
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"Users: <b>{int(u['c'])}</b>\n"
        f"Purchases: <b>{int(p['c'])}</b>\n"
        f"Paid: <b>{int(paid['c'])}</b>\n\n"
        f"DB: <code>{DB_PATH}</code>"
    )
    try:
        await call.message.edit_text(text, reply_markup=kb_admin_panel())
    except Exception:
        await call.message.answer(text, reply_markup=kb_admin_panel())
    await call.answer()

@router.callback_query(F.data == "admin_grant_help")
async def cb_admin_grant_help(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа.", show_alert=True)
        return
    text = (
        "✅ <b>Выдать доступ вручную</b>\n\n"
        "Команда:\n"
        "<code>/grant 123456789</code>\n"
        "или\n"
        "<code>/grant @username</code>\n\n"
        "Важно: пользователь должен сначала нажать /start, чтобы попасть в базу."
    )
    try:
        await call.message.edit_text(text, reply_markup=kb_admin_panel())
    except Exception:
        await call.message.answer(text, reply_markup=kb_admin_panel())
    await call.answer()

async def _find_user_by_identifier(identifier: str):
    identifier = identifier.strip()
    async with get_db() as db:
        if identifier.startswith("@"):
            username = identifier[1:]
            cur = await db.execute("SELECT * FROM users WHERE username=?", (username,))
            return await cur.fetchone()
        try:
            tg_id = int(identifier)
        except Exception:
            return None
        cur = await db.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))
        return await cur.fetchone()

@router.message(Command("grant"))
async def cmd_grant(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: <code>/grant 123456789</code> или <code>/grant @username</code>")
        return
    user = await _find_user_by_identifier(parts[1])
    if not user:
        await message.answer("Пользователь не найден. Пусть сначала нажмёт /start.")
        return
    await set_full_access(int(user["id"]), True)
    await message.answer("✅ Доступ выдан.")

# ---------------------------------------------------------------------------
# DB DIAGNOSTIC (admin only)
# ---------------------------------------------------------------------------

@router.message(Command("db"))
async def cmd_db(message: Message):
    if not is_admin(message.from_user.id):
        return
    try:
        size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    except Exception:
        size = -1
    async with get_db() as db:
        cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [r["name"] for r in await cur.fetchall()]
    await message.answer(
        "🗄 <b>DB INFO</b>\n\n"
        f"Path: <code>{DB_PATH}</code>\n"
        f"Exists: <b>{'yes' if os.path.exists(DB_PATH) else 'no'}</b>\n"
        f"Size: <b>{size}</b> bytes\n"
        f"Tables: <code>{', '.join(tables) if tables else 'none'}</code>\n\n"
        "Если ты ожидаешь, что база переживёт redeploy — подключи Volume и смонтируй в /data.",
        reply_markup=main_kb(),
    )

# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------

@router.message()
async def fallback(message: Message):
    if is_spam(message.from_user.id):
        return
    await message.answer("Используй кнопки снизу 👇", reply_markup=main_kb())

# ---------------------------------------------------------------------------
# START
# ---------------------------------------------------------------------------

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан. Добавь BOT_TOKEN в Railway Variables.")

    session = AiohttpSession(timeout=60)
    bot = Bot(
        BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher()
    dp.include_router(router)

    await init_db()
    await bot.delete_webhook(drop_pending_updates=True)

    await dp.start_polling(
        bot,
        allowed_updates=dp.resolve_used_update_types(),
        polling_timeout=30,
        request_timeout=65,
    )

if __name__ == "__main__":
    asyncio.run(main())
