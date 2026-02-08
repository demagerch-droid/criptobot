# -*- coding: utf-8 -*-


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
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ChatJoinRequest,
)

# ---------------------------------------------------------------------------
# НАСТРОЙКИ
# Лучше хранить токен в переменных окружения (Railway Variables), но оставил fallback.
# ---------------------------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "PASTE_BOT_TOKEN_HERE")  # поставь в Railway Variables
ADMIN_ID = int(os.getenv("ADMIN_ID", "8585550939"))                 # поставь в Railway Variables (числом)

# TronGrid / TRC20 (USDT)
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "")
WALLET_ADDRESS  = os.getenv("WALLET_ADDRESS", "")           # адрес получателя USDT TRC20 (T...)
USDT_TRON_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"  # стандартный контракт USDT TRC20

# Подписка (месячная)
PRICE_MONTH = Decimal(os.getenv("PRICE_MONTH", "20"))  # $20 / 30 дней
SUB_DAYS = int(os.getenv("SUB_DAYS", "30"))

# Куда вести после оплаты
PRIVATE_CHANNEL_URL = os.getenv("PRIVATE_CHANNEL_URL", "")  # канал отключён
COMMUNITY_GROUP_URL = os.getenv("COMMUNITY_GROUP_URL", "https://t.me/your_group_or_forum_link")
SUPPORT_CONTACT = os.getenv("SUPPORT_CONTACT", "@your_support_username")

# Авто-кик при окончании подписки (бот должен быть админом в чате)
# Укажи числовые chat_id (обычно начинаются с -100...). 0 = выключено.
PRIVATE_CHANNEL_ID = int(os.getenv("PRIVATE_CHANNEL_ID", "0"))
COMMUNITY_GROUP_ID = int(os.getenv("COMMUNITY_GROUP_ID", "0"))

# Логика напоминаний/проверок
KICK_ON_EXPIRE = os.getenv("KICK_ON_EXPIRE", "1") == "1"
REMIND_BEFORE_HOURS = int(os.getenv("REMIND_BEFORE_HOURS", "24"))  # напомнить за N часов
SUB_WATCH_INTERVAL_SEC = int(os.getenv("SUB_WATCH_INTERVAL_SEC", "600"))  # как часто проверять (сек)

# Антиспам (сек)
ANTISPAM_SECONDS = float(os.getenv("ANTISPAM_SECONDS", "1.2"))

# ---------------------------------------------------------------------------
# DB PATH (Railway Volume: /data)
# ---------------------------------------------------------------------------

DB_DIR = "/data" if os.path.isdir("/data") else os.path.dirname(os.path.abspath(__file__))
os.makedirs(DB_DIR, exist_ok=True)

DB_PATH = os.path.join(DB_DIR, "database.db")
print("DB_PATH =", DB_PATH)

# ---------------------------------------------------------------------------
# ОФОРМЛЕНИЕ / ТЕКСТЫ / МОДУЛИ
# ---------------------------------------------------------------------------

PROJECT_NAME = "Traffic Partner Bot"
ACCESS_NAME = "PRO подписка"

MODULES = []  # меню модулей отключено

MODULE_TEXT_PLACEHOLDER = (
    "📝 <b>Здесь будет текст модуля</b>\n\n"
    "Ты можешь вставить сюда свой контент, чек-листы, ссылки, примеры связок и т.д.\n"
    "Чтобы было красиво — делай:\n"
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
# DB HELPERS
# ---------------------------------------------------------------------------

@asynccontextmanager
async def get_db():
    """
    Единый способ открыть БД.
    Важно: row_factory включен, чтобы row["id"] работало.
    """
    db = await aiosqlite.connect(DB_PATH, timeout=30)
    db.row_factory = aiosqlite.Row
    # WAL обычно ок, но если увидишь "database is locked" — поменяй на DELETE
    await db.execute("PRAGMA journal_mode=WAL;")
    await db.execute("PRAGMA foreign_keys=ON;")
    await db.execute("PRAGMA busy_timeout=30000;")
    try:
        yield db
    finally:
        await db.close()


async def init_db():
    """
    Создание/миграция схемы.
    Чинит старую таблицу users без колонки id.
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
                    reg_date TEXT,
                    sub_until TEXT DEFAULT '',
                    free_trial_used INTEGER DEFAULT 0,
                    expire_24h_notified INTEGER DEFAULT 0,
                    expired_notified INTEGER DEFAULT 0,
                    kicked INTEGER DEFAULT 0,
                    -- legacy fields (оставлены для совместимости со старой БД)
                    referrer_id INTEGER,
                    full_access INTEGER DEFAULT 0,
                    balance TEXT DEFAULT '0',
                    total_earned TEXT DEFAULT '0',
                    is_blocked INTEGER DEFAULT 0,
                    FOREIGN KEY(referrer_id) REFERENCES users(id)
                );"""
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
                else:
                    logger.error("DB MIGRATION: can't find tg_id column in users_old, recreating empty users table.")

                await db.execute("DROP TABLE users_old;")
                await db.execute("PRAGMA foreign_keys=ON;")
                await db.commit()

        # --- миграция: добавляем новые колонки для подписки, если их нет
        cur = await db.execute("PRAGMA table_info(users)")
        cols2 = [r["name"] for r in await cur.fetchall()]
        if "sub_until" not in cols2:
            await db.execute("ALTER TABLE users ADD COLUMN sub_until TEXT DEFAULT ''")
        if "free_trial_used" not in cols2:
            await db.execute("ALTER TABLE users ADD COLUMN free_trial_used INTEGER DEFAULT 0")
        if "expire_24h_notified" not in cols2:
            await db.execute("ALTER TABLE users ADD COLUMN expire_24h_notified INTEGER DEFAULT 0")
        if "expired_notified" not in cols2:
            await db.execute("ALTER TABLE users ADD COLUMN expired_notified INTEGER DEFAULT 0")
        if "kicked" not in cols2:
            await db.execute("ALTER TABLE users ADD COLUMN kicked INTEGER DEFAULT 0")
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

        # ----------------------------
        # Withdrawals (вывод средств)
        # ----------------------------
        await db.execute(
            '''
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                tg_id INTEGER NOT NULL,
                amount TEXT NOT NULL,
                wallet TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                decided_at TEXT,
                decided_by INTEGER,
                comment TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            '''
        )

        await db.execute("CREATE INDEX IF NOT EXISTS idx_withdrawals_user_status ON withdrawals(user_id, status);")

        await db.execute(
            '''
            CREATE TABLE IF NOT EXISTS withdrawals_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                withdrawal_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                balance_before TEXT,
                balance_after TEXT,
                admin_id INTEGER,
                note TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(withdrawal_id) REFERENCES withdrawals(id) ON DELETE CASCADE
            );
            '''
        )

        await db.commit()

# ---------------------------------------------------------------------------
# USERS
# ---------------------------------------------------------------------------

async def get_user_by_tg(tg_id: int):
    async with get_db() as db:
        cur = await db.execute(
            """
            SELECT id, tg_id, username, first_name, reg_date, sub_until, free_trial_used, full_access, balance, total_earned, referrer_id, is_blocked
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



# ---------------------------------------------------------------------------
# Подписка
# ---------------------------------------------------------------------------

def _parse_dt(ts: str) -> datetime | None:
    ts = (ts or "").strip()
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None

def _fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")

async def get_sub_until(user_db_id: int) -> datetime | None:
    async with get_db() as db:
        cur = await db.execute("SELECT sub_until FROM users WHERE id = ?", (user_db_id,))
        row = await cur.fetchone()
        return _parse_dt(row["sub_until"]) if row else None

async def set_sub_until(user_db_id: int, until_dt: datetime | None):
    async with get_db() as db:
        await db.execute("UPDATE users SET sub_until = ? WHERE id = ?", (_fmt_dt(until_dt) if until_dt else "", user_db_id))
        await db.commit()

async def extend_subscription(user_db_id: int, days: int = SUB_DAYS):
    now = datetime.utcnow()
    current = await get_sub_until(user_db_id)
    base = current if (current and current > now) else now
    new_until = base + timedelta(days=days)
    await set_sub_until(user_db_id, new_until)
    return new_until

async def has_access_by_tg(tg_id: int) -> bool:
    """Совместимость: раньше был full_access, теперь — активная подписка."""
    row = await get_user_by_tg(tg_id)
    if not row:
        return False
    dt = _parse_dt(row["sub_until"]) if (hasattr(row, "keys") and "sub_until" in row.keys()) else None
    if not dt:
        return False
    return dt > datetime.utcnow()


# ---------------------------------------------------------------------------
# Авто-кик / напоминания по окончанию подписки
# ---------------------------------------------------------------------------

async def reset_expire_flags(user_db_id: int):
    async with get_db() as db:
        await db.execute(
            "UPDATE users SET expire_24h_notified = 0, expired_notified = 0, kicked = 0 WHERE id = ?",
            (user_db_id,),
        )
        await db.commit()

async def mark_expire_24h_notified(user_db_id: int):
    async with get_db() as db:
        await db.execute("UPDATE users SET expire_24h_notified = 1 WHERE id = ?", (user_db_id,))
        await db.commit()

async def mark_expired_notified(user_db_id: int):
    async with get_db() as db:
        await db.execute("UPDATE users SET expired_notified = 1 WHERE id = ?", (user_db_id,))
        await db.commit()

async def mark_kicked(user_db_id: int):
    async with get_db() as db:
        await db.execute("UPDATE users SET kicked = 1 WHERE id = ?", (user_db_id,))
        await db.commit()

async def _try_ban(bot: Bot, chat_id: int, tg_id: int) -> bool:
    if not chat_id:
        return False
    try:
        # Баним (чтобы не смог зайти обратно без разбанa)
        await bot.ban_chat_member(chat_id, tg_id)
        return True
    except Exception:
        return False

async def _try_unban(bot: Bot, chat_id: int, tg_id: int) -> bool:
    if not chat_id:
        return False
    try:
        await bot.unban_chat_member(chat_id, tg_id, only_if_banned=True)
        return True
    except Exception:
        return False

async def remind_and_kick_expired(bot: Bot):
    """Проверка подписок:
    1) Напоминание за REMIND_BEFORE_HOURS
    2) При окончании — кик/бан из чата и сообщение в боте
    """
    now = datetime.utcnow()
    now_ts = now.strftime("%Y-%m-%d %H:%M:%S")
    soon = now + timedelta(hours=REMIND_BEFORE_HOURS)
    soon_ts = soon.strftime("%Y-%m-%d %H:%M:%S")

    # 1) Напомнить, что скоро закончится
    async with get_db() as db:
        cur = await db.execute(
            """
            SELECT id, tg_id, sub_until
            FROM users
            WHERE sub_until != ''
              AND sub_until > ?
              AND sub_until <= ?
              AND COALESCE(expire_24h_notified, 0) = 0
            """,
            (now_ts, soon_ts),
        )
        soon_rows = await cur.fetchall()

    for r in soon_rows:
        uid = int(r["id"])
        tg_id = int(r["tg_id"])
        sub_until = _parse_dt(r["sub_until"])
        if not sub_until:
            await mark_expire_24h_notified(uid)
            continue
        try:
            await bot.send_message(
                tg_id,
                f"""⏳ <b>Подписка скоро закончится</b>

До окончания осталось меньше <b>{REMIND_BEFORE_HOURS} ч.</b>
🗓 Дата окончания: <b>{sub_until.strftime('%d.%m.%Y %H:%M')} UTC</b>

Чтобы не потерять доступ к закрытому чату — продли подписку ⭐️
""",
                reply_markup=main_kb(),
            )
        except Exception:
            pass
        await mark_expire_24h_notified(uid)

    # 2) Истекшие — кикнуть + напомнить оплатить
    async with get_db() as db:
        cur = await db.execute(
            """
            SELECT id, tg_id, sub_until
            FROM users
            WHERE sub_until != ''
              AND sub_until <= ?
              AND COALESCE(kicked, 0) = 0
            """,
            (now_ts,),
        )
        expired_rows = await cur.fetchall()

    for r in expired_rows:
        uid = int(r["id"])
        tg_id = int(r["tg_id"])

        kicked_any = False
        if KICK_ON_EXPIRE:            kicked_any = (await _try_ban(bot, COMMUNITY_GROUP_ID, tg_id)) or kicked_any

        # Сообщение пользователю
        try:
            await bot.send_message(
                tg_id,
                """⛔️ <b>Подписка закончилась</b>

🚫 Доступ к закрытому чату приостановлен.

Чтобы вернуть доступ — продли подписку на месяц 👇

⭐️ <b>Подписка</b> → 💳 <b>Оформить</b> → ✅ <b>Проверить оплату</b>
""",
                reply_markup=main_kb(),
            )
        except Exception:
            pass

        await mark_expired_notified(uid)

        # kicked=1 ставим только если есть куда кикать (чтобы не “сломать” логику при пустых chat_id)
        if KICK_ON_EXPIRE and (COMMUNITY_GROUP_ID):
            await mark_kicked(uid)

async def subscription_watcher(bot: Bot):
    while True:
        try:
            await remind_and_kick_expired(bot)
        except Exception as e:
            logger.exception("subscription_watcher error: %s", e)
        await asyncio.sleep(max(30, SUB_WATCH_INTERVAL_SEC))

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




# ---------------------------------------------------------------------------
# WITHDRAWALS (вывод средств) — Вариант A: “заморозка” при заявке
# ---------------------------------------------------------------------------

# Пользователь нажал “Вывести” и мы ждём, пока он пришлёт кошелёк одним сообщением.
# Ключ: tg_id пользователя -> datetime (когда начал процесс)
WAITING_WITHDRAW_WALLET: dict[int, datetime] = {}


def _now_ts() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _q2(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_DOWN)


async def get_active_withdrawal(user_db_id: int):
    async with get_db() as db:
        cur = await db.execute(
            "SELECT * FROM withdrawals WHERE user_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1",
            (user_db_id,),
        )
        return await cur.fetchone()


async def create_withdrawal_freeze(user_db_id: int, tg_id: int, wallet: str):
    """
    Создаёт заявку withdrawals со статусом pending и сразу “замораживает” сумму:
    - списывает весь доступный balance у пользователя
    - пишет запись в withdrawals_log
    Всё делается атомарно (BEGIN IMMEDIATE).
    Возвращает: (withdrawal_row | None, error_code | None)
      error_code: 'active' | 'zero' | None
    """
    wallet = (wallet or "").strip()
    async with get_db() as db:
        try:
            await db.execute("BEGIN IMMEDIATE;")

            # защита от повторных заявок
            cur = await db.execute(
                "SELECT id FROM withdrawals WHERE user_id = ? AND status = 'pending' LIMIT 1",
                (user_db_id,),
            )
            if await cur.fetchone():
                await db.execute("ROLLBACK;")
                return None, "active"

            cur = await db.execute("SELECT balance FROM users WHERE id = ?", (user_db_id,))
            u = await cur.fetchone()
            if not u:
                await db.execute("ROLLBACK;")
                return None, "zero"

            bal_before = Decimal(u["balance"])
            if bal_before <= 0:
                await db.execute("ROLLBACK;")
                return None, "zero"

            amount = _q2(bal_before)  # замораживаем весь доступный баланс
            bal_after = _q2(bal_before - amount)

            await db.execute(
                "UPDATE users SET balance = ? WHERE id = ?",
                (str(bal_after), user_db_id),
            )

            created_at = _now_ts()
            cur2 = await db.execute(
                """
                INSERT INTO withdrawals (user_id, tg_id, amount, wallet, status, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (user_db_id, tg_id, str(amount), wallet, created_at),
            )
            withdrawal_id = cur2.lastrowid

            await db.execute(
                """
                INSERT INTO withdrawals_log (withdrawal_id, action, balance_before, balance_after, admin_id, note, created_at)
                VALUES (?, 'create', ?, ?, NULL, ?, ?)
                """,
                (withdrawal_id, str(bal_before), str(bal_after), "freeze_on_request", created_at),
            )

            await db.commit()

            cur3 = await db.execute("SELECT * FROM withdrawals WHERE id = ?", (withdrawal_id,))
            row = await cur3.fetchone()
            return row, None
        except Exception:
            try:
                await db.execute("ROLLBACK;")
            except Exception:
                pass
            raise


async def get_withdrawal_by_id(withdrawal_id: int):
    async with get_db() as db:
        cur = await db.execute("SELECT * FROM withdrawals WHERE id = ?", (withdrawal_id,))
        return await cur.fetchone()


async def admin_mark_withdrawal_paid(withdrawal_id: int, admin_tg_id: int):
    """
    Админ нажал ✅ Оплачено:
    - меняем статус pending -> paid
    - пишем лог
    Атомарно.
    Возвращает: (row_before | None, error_code | None)
      error_code: 'not_found' | 'already'
    """
    async with get_db() as db:
        try:
            await db.execute("BEGIN IMMEDIATE;")
            cur = await db.execute("SELECT * FROM withdrawals WHERE id = ?", (withdrawal_id,))
            wd = await cur.fetchone()
            if not wd:
                await db.execute("ROLLBACK;")
                return None, "not_found"
            if wd["status"] != "pending":
                await db.execute("ROLLBACK;")
                return wd, "already"

            decided_at = _now_ts()
            await db.execute(
                """
                UPDATE withdrawals
                SET status = 'paid', decided_at = ?, decided_by = ?
                WHERE id = ?
                """,
                (decided_at, admin_tg_id, withdrawal_id),
            )

            await db.execute(
                """
                INSERT INTO withdrawals_log (withdrawal_id, action, balance_before, balance_after, admin_id, note, created_at)
                VALUES (?, 'paid', NULL, NULL, ?, NULL, ?)
                """,
                (withdrawal_id, admin_tg_id, decided_at),
            )

            await db.commit()
            return wd, None
        except Exception:
            try:
                await db.execute("ROLLBACK;")
            except Exception:
                pass
            raise


async def admin_decline_withdrawal(withdrawal_id: int, admin_tg_id: int, comment: str = ""):
    """
    Админ нажал ❌ Отклонить:
    - pending -> declined
    - возвращаем замороженную сумму обратно в users.balance
    - пишем лог
    Атомарно.
    Возвращает: (wd_row_before | None, error_code | None)
      error_code: 'not_found' | 'already'
    """
    comment = (comment or "").strip()
    async with get_db() as db:
        try:
            await db.execute("BEGIN IMMEDIATE;")

            cur = await db.execute("SELECT * FROM withdrawals WHERE id = ?", (withdrawal_id,))
            wd = await cur.fetchone()
            if not wd:
                await db.execute("ROLLBACK;")
                return None, "not_found"
            if wd["status"] != "pending":
                await db.execute("ROLLBACK;")
                return wd, "already"

            amount = Decimal(wd["amount"])

            cur2 = await db.execute("SELECT balance FROM users WHERE id = ?", (int(wd["user_id"]),))
            u = await cur2.fetchone()
            bal_before = Decimal(u["balance"]) if u else Decimal("0")
            bal_after = _q2(bal_before + amount)

            await db.execute(
                "UPDATE users SET balance = ? WHERE id = ?",
                (str(bal_after), int(wd["user_id"])),
            )

            decided_at = _now_ts()
            await db.execute(
                """
                UPDATE withdrawals
                SET status = 'declined', decided_at = ?, decided_by = ?, comment = ?
                WHERE id = ?
                """,
                (decided_at, admin_tg_id, comment, withdrawal_id),
            )

            await db.execute(
                """
                INSERT INTO withdrawals_log (withdrawal_id, action, balance_before, balance_after, admin_id, note, created_at)
                VALUES (?, 'declined', ?, ?, ?, ?, ?)
                """,
                (withdrawal_id, str(bal_before), str(bal_after), admin_tg_id, comment or "declined", decided_at),
            )

            await db.commit()
            return wd, None
        except Exception:
            try:
                await db.execute("ROLLBACK;")
            except Exception:
                pass
            raise

async def count_referrals_clicks(user_db_id: int):
    """Сколько пользователей пришло по реф-ссылке (по факту /start с ref)."""
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


async def count_referrals(user_db_id: int):
    """Сколько рефералов оплатили доступ (1 и 2 линия)."""
    async with get_db() as db:
        # 1 линия: пришли по твоей ссылке и оплатили access
        cur1 = await db.execute(
            """
            SELECT COUNT(DISTINCT u.id) AS c
            FROM users u
            WHERE u.referrer_id = ?
              AND EXISTS (
                SELECT 1 FROM purchases p
                WHERE p.user_id = u.id
                  AND p.status = 'paid'
                  AND p.product_code = 'sub_month'
              )
            """,
            (user_db_id,),
        )
        lvl1 = (await cur1.fetchone())["c"]

        # 2 линия: пришли по ссылке твоих рефералов 1 линии и оплатили access
        cur2 = await db.execute(
            """
            SELECT COUNT(DISTINCT u.id) AS c
            FROM users u
            WHERE u.referrer_id IN (SELECT id FROM users WHERE referrer_id = ?)
              AND EXISTS (
                SELECT 1 FROM purchases p
                WHERE p.user_id = u.id
                  AND p.status = 'paid'
                  AND p.product_code = 'sub_month'
              )
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
            "UPDATE purchases SET status='paid', paid_at=?, tx_id=? WHERE id=?",
            (paid_at, tx_id, purchase_id),
        )
        await db.commit()


async def get_tg_id_by_user_db(user_db_id: int) -> int | None:
    async with get_db() as db:
        cur = await db.execute("SELECT tg_id FROM users WHERE id = ?", (user_db_id,))
        row = await cur.fetchone()
        return int(row["tg_id"]) if row else None


async def process_successful_payment(bot: Bot, purchase_row):
    user_db_id = int(purchase_row["user_id"])
    product_code = purchase_row["product_code"]

    if product_code != "sub_month":
        return

    new_until = await extend_subscription(user_db_id, SUB_DAYS)

    # Сбрасываем флаги окончания подписки и (если был бан) разбаниваем
    await reset_expire_flags(user_db_id)

    buyer_tg_id = await get_tg_id_by_user_db(user_db_id)
    if buyer_tg_id:
        # Если пользователь был забанен по окончанию подписки — разбаниваем
        await _try_unban(bot, COMMUNITY_GROUP_ID, buyer_tg_id)

        text = f"""✅ <b>Оплата подтверждена!</b>

⭐️ Подписка активна до: <b>{new_until.strftime('%d.%m.%Y %H:%M')} UTC</b>

Теперь тебе доступно:
📚 материалы и инструкции
💬 вход в закрытый чат

Жми <b>«🧠 Обучение»</b> — там кнопка на чат 👇"""
        await bot.send_message(
            buyer_tg_id,
            text,
            reply_markup=main_kb(),
        )



async def fetch_trc20_transactions() -> list:
    if not WALLET_ADDRESS:
        logger.error("WALLET_ADDRESS пустой. В Railway Variables задай WALLET_ADDRESS.")
        return []

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

            if abs(value - amount) > Decimal("0.0005"):
                continue

            ts_ms = tx.get("block_timestamp")
            tx_time = datetime.utcfromtimestamp(ts_ms / 1000.0)

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
                KeyboardButton(text="⭐️ Подписка"),
                KeyboardButton(text="👤 Профиль"),
            ]
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери раздел 👇",
    )


def kb_back(cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=cb)]])


def kb_buy(back_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"💳 Оформить подписку ({PRICE_MONTH}$ / {SUB_DAYS}д)", callback_data="buy_access")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)],
        ]
    )


def kb_training(has_access: bool) -> InlineKeyboardMarkup:
    """Кнопки для раздела обучения.
    Модули убраны: оставляем ссылку на закрытый чат (если подписка активна) или оплату (если нет).
    """
    rows = []
    if has_access:
        if COMMUNITY_GROUP_URL:
            rows.append([InlineKeyboardButton(text="💬 Закрытый чат", url=COMMUNITY_GROUP_URL)])
    else:
        rows.append([InlineKeyboardButton(text=f"💳 Оформить подписку ({PRICE_MONTH}$ / {SUB_DAYS}д)", callback_data="buy_access")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_earn(has_access: bool) -> InlineKeyboardMarkup:
    if not has_access:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📌 Как работает партнёрка", callback_data="earn_info")],
                [InlineKeyboardButton(text=f"💳 Оформить подписку ({PRICE_MONTH}$)", callback_data="buy_access")],
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


def kb_profile(has_access: bool, is_admin_flag: bool) -> InlineKeyboardMarkup:
    rows = []

    # подписка
    if has_access:
        rows.append([InlineKeyboardButton(text="⭐️ Подписка (активна)", callback_data="open_sub")])
    else:
        rows.append([InlineKeyboardButton(text="⭐️ Подписка (оформить)", callback_data="open_sub")])

    rows.append([InlineKeyboardButton(text="ℹ️ FAQ", callback_data="faq")])
    rows.append([InlineKeyboardButton(text="💬 Поддержка", callback_data="support")])

    if is_admin_flag:
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
# Защита закрытого чата (Join Requests)
# Если кто-то поделится ссылкой — бот одобрит заявку только при активной подписке.
# ---------------------------------------------------------------------------

@router.chat_join_request()
async def on_chat_join_request(req: ChatJoinRequest):
    # работаем только с основным чатом (COMMUNITY_GROUP_ID)
    if not COMMUNITY_GROUP_ID or req.chat.id != COMMUNITY_GROUP_ID:
        return

    tg_id = req.from_user.id
    has = await has_access_by_tg(tg_id)

    try:
        if has:
            await req.bot.approve_chat_join_request(chat_id=req.chat.id, user_id=tg_id)
            # по желанию — приветствие в личку
            try:
                await req.bot.send_message(
                    tg_id,
                    "✅ Заявка одобрена! Добро пожаловать в закрытый чат 💬",
                    reply_markup=main_kb(),
                )
            except Exception:
                pass
        else:
            await req.bot.decline_chat_join_request(chat_id=req.chat.id, user_id=tg_id)
            # по желанию — объяснение в личку (может не дойти, если пользователь не нажимал /start)
            try:
                await req.bot.send_message(
                    tg_id,
                    "⛔️ Доступ в закрытый чат только по активной подписке.\n\n"
                    "Нажми ⭐️ <b>Подписка</b> → 💳 <b>Оформить</b> → ✅ <b>Проверить оплату</b>.",
                    reply_markup=main_kb(),
                )
            except Exception:
                pass
    except Exception:
        # если нет прав у бота — просто молча игнорируем
        return


# ---------------------------------------------------------------------------
# Основные экраны
# ---------------------------------------------------------------------------

async def show_home(message: Message):
    # Главный экран (без рефералок, с подпиской)
    row = await get_user_by_tg(message.from_user.id)
    sub_until = _parse_dt(row["sub_until"]) if row and "sub_until" in row.keys() else None
    active = bool(sub_until and sub_until > datetime.utcnow())

    status = f"Активна до <b>{sub_until.strftime('%d.%m.%Y %H:%M')} UTC</b> ✅" if active else "Не активна ❌"

    text = f"""⚡️ <b>{PROJECT_NAME}</b>

🚀 Закрытое комьюнити по трафику: коротко, по делу и с регулярными обновлениями.

<b>Что внутри подписки:</b>
📌 Пошаговые гайды: прогрев → креатив → залив → Telegram
🧩 Шаблоны/чек-листы + примеры связок
🔥 Еженедельные обновления и разборы
💬 Закрытый чат для вопросов и поддержки

⭐️ <b>Подписка:</b> <b>{PRICE_MONTH}$</b> / <b>{SUB_DAYS} дней</b> (USDT TRC20)
🧾 <b>Статус:</b> {status}

👇 Выбирай раздел снизу:"""
    await message.answer(text, reply_markup=main_kb())


async def show_training(target: Message | CallbackQuery, edit: bool = False):
    if isinstance(target, CallbackQuery):
        tg_id = target.from_user.id
        msg = target.message
    else:
        tg_id = target.from_user.id
        msg = target

    has = await has_access_by_tg(tg_id)

    if has:
        text = """🧠 <b>Обучение</b>

✅ <b>Подписка активна</b>

📚 Внутри:
• 🎯 связки и примеры воронок
• 🎬 креативы, скрипты, оформление
• 📊 аналитика и контроль цифр
• 🧠 ошибки новичков и как их обходить

💬 Ниже — кнопка для входа в закрытый чат 👇"""
    else:
        text = """🧠 <b>Обучение</b>

🚀 Это база по переливу трафика: прогрев, креативы, аналитика, переход в Telegram и масштабирование.

<b>После оплаты ты получаешь:</b>
🔒 доступ в закрытый чат
📌 материалы и инструкции (пошагово)
🔥 обновления и разборы

Нажми кнопку ниже — бот даст сумму и кошелёк для оплаты ✅"""

    kb = kb_training(has)

    if edit and isinstance(target, CallbackQuery):
        try:
            await msg.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass

    await msg.answer(text, reply_markup=kb)



async def show_subscription(target: Message | CallbackQuery, edit: bool = False):
    if isinstance(target, CallbackQuery):
        tg_id = target.from_user.id
        msg = target.message
    else:
        tg_id = target.from_user.id
        msg = target

    row = await get_user_by_tg(tg_id)
    sub_until = _parse_dt(row["sub_until"]) if row and "sub_until" in row.keys() else None
    now = datetime.utcnow()
    active = bool(sub_until and sub_until > now)

    if active:
        left_days = max((sub_until - now).days, 0)
        text = (
            "⭐️ <b>Подписка</b>\n\n"
            f"✅ Активна до: <b>{sub_until.strftime('%d.%m.%Y %H:%M')} UTC</b>\n"
            f"⏳ Осталось примерно: <b>{left_days} дн.</b>\n\n"
            "<b>Что входит:</b>\n"
            "📚 материалы и пошаговые инструкции\n"
            "🔥 обновления и разборы\n"
            "💬 доступ в закрытый чат\n\n"
            f"Тариф: <b>{PRICE_MONTH}$</b> / <b>{SUB_DAYS} дней</b>\n\n"
            "Можно продлить заранее — дни просто прибавятся."
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"🔄 Продлить ({PRICE_MONTH}$ / {SUB_DAYS}д)", callback_data="buy_access")],
            ]
        )
    else:
        text = (
            "⭐️ <b>Подписка</b>\n\n"
            "❌ Сейчас подписка не активна.\n\n"
            "<b>Что входит:</b>\n"
            "📚 материалы и инструкции (пошагово)\n"
            "💬 доступ в закрытый чат\n"
            "🔥 обновления и разборы\n\n"
            f"Тариф: <b>{PRICE_MONTH}$</b> / <b>{SUB_DAYS} дней</b>\n"
            "Оплата: <b>USDT (TRC20)</b>\n\n"
            "Нажми кнопку ниже — бот выдаст точную сумму и кошелёк.\n"
            "После оплаты нажми «Проверить оплату» — доступ откроется автоматически."
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"💳 Оформить ({PRICE_MONTH}$ / {SUB_DAYS}д)", callback_data="buy_access")],
            ]
        )

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
            "• <b>50%</b> с 1-й линии\n"
            "• <b>10%</b> со 2-й линии\n\n"
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
        tg_user = target.from_user
    else:
        tg_id = target.from_user.id
        msg = target
        tg_user = target.from_user

    row = await get_user_by_tg(tg_id)
    if not row:
        await get_or_create_user(tg_user, None)
        row = await get_user_by_tg(tg_id)

    username = row["username"] or ""
    first_name = row["first_name"] or ""
    reg_date = row["reg_date"] or "—"

    sub_until = _parse_dt(row["sub_until"]) if "sub_until" in row.keys() else None
    now = datetime.utcnow()
    active = bool(sub_until and sub_until > now)
    status = f"Активна до <b>{sub_until.strftime('%d.%m.%Y %H:%M')} UTC</b> ✅" if active else "Не активна ❌"

    text = f"""👤 <b>Профиль</b>

👋 Имя: <b>{first_name or '—'}</b>
🔹 Username: @{username if username else '—'}
🆔 ID: <code>{tg_id}</code>
📅 Регистрация: <b>{reg_date}</b>

⭐️ <b>Подписка:</b> {status}"""

    kb = kb_profile(active, is_admin(tg_id))

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

    await get_or_create_user(message.from_user, None)
    await show_home(message)

# ---------------------------------------------------------------------------
# Нижнее меню
# ---------------------------------------------------------------------------

@router.message(F.text == "🧠 Обучение")
async def menu_training(message: Message):
    if is_spam(message.from_user.id):
        return
    await show_training(message)

@router.message(F.text == "⭐️ Подписка")
async def menu_subscription(message: Message):
    if is_spam(message.from_user.id):
        return
    await show_subscription(message)

@router.message(F.text == "👤 Профиль")
async def menu_profile(message: Message):
    if is_spam(message.from_user.id):
        return
    await show_profile(message)

# ---------------------------------------------------------------------------
# Обучение: модули
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

    if not MODULES:
        await call.answer("🧠 Модули сейчас отключены.", show_alert=True)
        await show_training(call, edit=True)
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
        f"✅ После оплаты доступа (<b>{PRICE_MONTH}$</b>) ты получаешь:\n"
        "• личную реферальную ссылку\n"
        "• статистику по партнёрам\n"
        "• начисления на баланс\n\n"
        "💰 Начисления:\n"
        "• <b>50%</b> с 1-й линии\n"
        "• <b>10%</b> со 2-й линии\n\n"
        "⚠️ Начисления идут только с покупки полного доступа."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оформить подписку", callback_data="buy_access")],
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
        text = "🔗 <b>Реферальная ссылка</b>\n\nСначала открой полный доступ — и здесь появится твоя реф-ссылка + статистика."
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
    click1, _click2 = await count_referrals_clicks(user_db_id)
    lvl1, lvl2 = await count_referrals(user_db_id)
    balance = Decimal(row["balance"])
    total_earned = Decimal(row["total_earned"])
    access = bool(row["full_access"])

    text = (
        "📊 <b>Моя статистика</b>\n\n"
        f"Доступ: <b>{'Открыт ✅' if access else 'Не оплачен ❌'}</b>\n\n"
        f"👤 Перешли по ссылке: <b>{click1}</b>\n\n"
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
    if is_spam(call.from_user.id):
        return

    tg_id = call.from_user.id
    user = await get_user_by_tg(tg_id)
    if not user:
        await call.answer("Сначала нажми /start 🙂", show_alert=True)
        return

    if not user["full_access"]:
        await call.answer("У тебя нет полного доступа.", show_alert=True)
        return

    # защита от повторных заявок
    active = await get_active_withdrawal(user["id"])
    if active:
        await call.answer("У тебя уже есть активная заявка на вывод ⏳", show_alert=True)
        return

    bal = Decimal(user["balance"])
    if bal <= 0:
        await call.answer("У тебя пока нет доступного баланса для вывода 🙂", show_alert=True)
        return

    WAITING_WITHDRAW_WALLET[tg_id] = datetime.utcnow()

    await call.message.answer(
        f"""💸 <b>Вывод средств</b>

Твой доступный баланс: <b>{bal}$</b>

Отправь одним сообщением свой <b>USDT-адрес (TRC20)</b>.
После этого заявка будет создана, а сумма — <b>заморожена</b> до решения администратора 🙂

Если передумал — напиши <b>отмена</b>."""
    )
    await call.answer()

# ---------------------------------------------------------------------------
# FAQ / Support
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "faq")
async def cb_faq(call: CallbackQuery):
    text = f"""ℹ️ <b>FAQ</b>

❓ <b>Сколько стоит подписка?</b>
• <b>{PRICE_MONTH}$</b> / <b>{SUB_DAYS} дней</b>

❓ <b>Что даёт подписка?</b>
• доступ к материалам и инструкциям
• доступ к закрытому чату

❓ <b>Можно продлить заранее?</b>
Да. Если подписка активна, при оплате дни просто прибавятся.

❓ <b>Что если оплатил, а доступ не открылся?</b>
Нажми «Проверить оплату». Иногда сеть задерживает транзакцию 1–3 минуты.
Если всё равно нет — напиши в поддержку: {SUPPORT_CONTACT}

⚠️ <b>Важно</b>
Бот — инструмент. Результат зависит от твоих действий."""
    try:
        await call.message.edit_text(text, reply_markup=kb_back("back:profile"))
    except Exception:
        await call.message.answer(text, reply_markup=kb_back("back:profile"))
    await call.answer()

def _looks_like_trc20(wallet: str) -> bool:
    w = (wallet or "").strip()
    # Простейшая проверка TRC20-адреса (обычно начинается на 'T')
    if len(w) < 26 or len(w) > 60:
        return False
    if not w.startswith("T"):
        return False
    return all(ch.isalnum() for ch in w)


@router.callback_query(F.data.startswith("wd_ok:"))
async def cb_withdraw_ok(call: CallbackQuery):
    if is_spam(call.from_user.id):
        return
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа.", show_alert=True)
        return

    try:
        withdrawal_id = int(call.data.split(":", 1)[1])
    except Exception:
        await call.answer("Ошибка данных.", show_alert=True)
        return

    wd, err = await admin_mark_withdrawal_paid(withdrawal_id, call.from_user.id)
    if err == "not_found":
        await call.answer("Заявка не найдена.", show_alert=True)
        return
    if err == "already":
        await call.answer("Эта заявка уже обработана.", show_alert=True)
        return

    # уведомляем пользователя
    try:
        await call.bot.send_message(
            int(wd["tg_id"]),
            f"""✅ <b>Заявка на вывод оплачена</b>

Сумма: <b>{wd['amount']}$</b>
Если оплата не дошла — напиши администратору 🙂""",
        )
    except Exception:
        pass

    # помечаем сообщение админа
    try:
        await call.message.edit_text((call.message.text or "") + "\n\n✅ <b>Статус:</b> ОПЛАЧЕНО")
    except Exception:
        pass

    await call.answer("Оплачено ✅")


@router.callback_query(F.data.startswith("wd_no:"))
async def cb_withdraw_decline(call: CallbackQuery):
    if is_spam(call.from_user.id):
        return
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа.", show_alert=True)
        return

    try:
        withdrawal_id = int(call.data.split(":", 1)[1])
    except Exception:
        await call.answer("Ошибка данных.", show_alert=True)
        return

    wd, err = await admin_decline_withdrawal(withdrawal_id, call.from_user.id)
    if err == "not_found":
        await call.answer("Заявка не найдена.", show_alert=True)
        return
    if err == "already":
        await call.answer("Эта заявка уже обработана.", show_alert=True)
        return

    # уведомляем пользователя + сумма вернулась на баланс
    try:
        await call.bot.send_message(
            int(wd["tg_id"]),
            f"""❌ <b>Заявка на вывод отклонена</b>

Сумма <b>{wd['amount']}$</b> возвращена на баланс.
Если думаешь, что это ошибка — напиши администратору 🙂""",
        )
    except Exception:
        pass

    # помечаем сообщение админа
    try:
        await call.message.edit_text((call.message.text or "") + "\n\n❌ <b>Статус:</b> ОТКЛОНЕНО")
    except Exception:
        pass

    await call.answer("Отклонено ❌")

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

@router.callback_query(F.data == "open_sub")
async def cb_open_sub(call: CallbackQuery):
    await show_subscription(call, edit=True)
    await call.answer()


# ---------------------------------------------------------------------------
# Покупка доступа / Проверка оплаты
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "buy_access")
async def cb_buy_access(call: CallbackQuery):
    user_row = await get_user_by_tg(call.from_user.id)
    if not user_row:
        await get_or_create_user(call.from_user, None)
        user_row = await get_user_by_tg(call.from_user.id)

    user_db_id = int(user_row["id"])
    purchase_id = await create_purchase(user_db_id, "sub_month", PRICE_MONTH)
    purchase = await get_purchase(purchase_id)
    amount = Decimal(purchase["amount"])

    text = (
        f"💳 <b>Оплата подписки ({PRICE_MONTH}$ / {SUB_DAYS} дней)</b>\n\n"
        "Оплата в <b>USDT (TRC20)</b>.\n\n"
        f"Кошелёк для оплаты:\n<code>{WALLET_ADDRESS or '— не задан —'}</code>\n\n"
        f"Сумма к оплате: <b>{amount} USDT</b>\n\n"
        "⚠️ Важно: отправь <b>ТОЧНО</b> эту сумму (с хвостиком) и внимательно посчитай комисию, иначе бот не сопоставит платёж.\n\n"
        "После оплаты нажми «Проверить оплату»."
    )

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
# Admin panel (минимальный)
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "admin_panel")
async def cb_admin_panel(call: CallbackQuery):
    """Открывает админ-панель по кнопке в профиле."""
    if not is_admin(call.from_user.id):
        await call.answer("⛔️ Нет доступа", show_alert=True)
        return

    text = (
        "🔐 <b>Админ-панель</b>\n\n"
        "Команды администратора:\n"
        "• <code>/grant 123456789</code> — выдать доступ по TG ID\n"
        "• <code>/grant @username</code> — выдать доступ по username\n\n"
        "Выбери действие ниже:"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton(text="✅ Как выдать доступ", callback_data="admin_grant_help")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:profile")],
        ]
    )

    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)

    await call.answer()


@router.callback_query(F.data == "admin_grant_help")
async def cb_admin_grant_help(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔️ Нет доступа", show_alert=True)
        return

    text = (
        "✅ <b>Как выдать подписку</b>\n\n"
        "1) Пользователь должен хотя бы 1 раз нажать /start (чтобы попал в базу).\n"
        "2) Затем ты в личке с ботом пишешь команду:\n\n"
        "• <code>/grant 123456789</code>\n"
        "или\n"
        "• <code>/grant @username</code>\n"
        "или\n"
        "• <code>/grant 123456789 90</code>\n\n"
        "После этого у пользователя продлится подписка (дни прибавятся)."
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")],
            [InlineKeyboardButton(text="↩️ В профиль", callback_data="back:profile")],
        ]
    )

    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)

    await call.answer()


@router.callback_query(F.data == "admin_stats")
async def cb_admin_stats(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔️ Нет доступа", show_alert=True)
        return

    async with get_db() as db:
        cur = await db.execute("SELECT COUNT(*) AS c FROM users")
        total_users = (await cur.fetchone())["c"]

        now_ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

        cur = await db.execute("SELECT COUNT(*) AS c FROM users WHERE sub_until > ?", (now_ts,))
        active_subs = (await cur.fetchone())["c"]

        cur = await db.execute("SELECT COUNT(*) AS c FROM purchases WHERE status = 'pending'")
        pending_pays = (await cur.fetchone())["c"]

        cur = await db.execute("SELECT COUNT(*) AS c FROM purchases WHERE status = 'paid'")
        paid_pays = (await cur.fetchone())["c"]

    text = (
        "📊 <b>Статистика</b>\n\n"
        f"👥 Всего пользователей: <b>{total_users}</b>\n"
        f"⭐️ Активных подписок: <b>{active_subs}</b>\n\n"
        f"⏳ Платежи pending: <b>{pending_pays}</b>\n"
        f"💳 Платежи paid: <b>{paid_pays}</b>"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")],
            [InlineKeyboardButton(text="↩️ В профиль", callback_data="back:profile")],
        ]
    )

    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)

    await call.answer()


async def _find_user_by_identifier(identifier: str):
    identifier = identifier.strip()
    async with get_db() as db:
        if identifier.startswith("@"):
            username = identifier[1:]
            cur = await db.execute("SELECT * FROM users WHERE username = ?", (username,))
            return await cur.fetchone()
        try:
            tg_id = int(identifier)
        except Exception:
            return None
        cur = await db.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
        return await cur.fetchone()

@router.message(Command("grant"))
async def cmd_grant(message: Message):
    """
    Выдача/продление подписки админом.
    Примеры:
      /grant 123456789
      /grant @username
      /grant 123456789 90
    """
    if not is_admin(message.from_user.id):
        return

    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2:
        await message.answer(
            "Использование:\n"
            "• <code>/grant 123456789</code>\n"
            "• <code>/grant @username</code>\n"
            "• <code>/grant 123456789 90</code> (дни)\n"
        )
        return

    identifier = parts[1]
    days = SUB_DAYS
    if len(parts) >= 3:
        try:
            days = int(parts[2])
            days = max(1, min(days, 3650))
        except Exception:
            days = SUB_DAYS

    user = await _find_user_by_identifier(identifier)
    if not user:
        await message.answer("Пользователь не найден в базе. Пусть сначала нажмёт /start.")
        return

    new_until = await extend_subscription(int(user["id"]), days)
    tg_id = int(user["tg_id"])

    # Сбрасываем флаги окончания подписки и (если был бан) разбаниваем
    await reset_expire_flags(int(user["id"]))
    await _try_unban(message.bot, COMMUNITY_GROUP_ID, tg_id)

    await message.answer(
        f"✅ Подписка продлена.\n"
        f"TG ID: <code>{tg_id}</code>\n"
        f"До: <b>{new_until.strftime('%d.%m.%Y %H:%M')} UTC</b>"
    )

# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------

@router.message(Command("id"))
async def cmd_id(message: Message):
    lines = [
        f"🆔 <b>Chat ID</b>: <code>{message.chat.id}</code>",
        f"📌 <b>Тип</b>: <code>{message.chat.type}</code>",
    ]

    # Если это пересланное сообщение (из чата) — покажем откуда
    fchat = getattr(message, "forward_from_chat", None)
    if fchat:
        lines.append(f"\n📩 <b>Переслано из</b>: <code>{fchat.id}</code> ({fchat.type})")

    forigin = getattr(message, "forward_origin", None)
    if forigin and getattr(forigin, "chat", None):
        ch = forigin.chat
        lines.append(f"\n📩 <b>Переслано из</b>: <code>{ch.id}</code> ({ch.type})")

    await message.answer("\n".join(lines))


@router.message(F.text)
async def handle_withdraw_wallet_input(message: Message):
    """Ловим кошелёк после нажатия “Вывести” (вариант A: заморозка при заявке)."""
    tg_id = message.from_user.id
    if tg_id not in WAITING_WITHDRAW_WALLET:
        return

    txt = (message.text or "").strip()
    low = txt.lower()

    if low in ("отмена", "cancel", "стоп"):
        WAITING_WITHDRAW_WALLET.pop(tg_id, None)
        await message.answer("Ок, отменено ✅")
        return

    if not _looks_like_trc20(txt):
        await message.answer(
            """Похоже, адрес неверный 😅
Пришли ещё раз <b>USDT-адрес (TRC20)</b> (обычно начинается на <b>T</b>)."""
        )
        return

    user = await get_user_by_tg(tg_id)
    if not user:
        WAITING_WITHDRAW_WALLET.pop(tg_id, None)
        await message.answer("Сначала нажми /start 🙂")
        return

    if not user["full_access"]:
        WAITING_WITHDRAW_WALLET.pop(tg_id, None)
        await message.answer("У тебя нет полного доступа.")
        return

    wd, err = await create_withdrawal_freeze(user["id"], tg_id, txt)
    WAITING_WITHDRAW_WALLET.pop(tg_id, None)

    if err == "active":
        await message.answer("У тебя уже есть активная заявка на вывод ⏳")
        return
    if err == "zero":
        await message.answer("У тебя пока нет доступного баланса для вывода 🙂")
        return

    await message.answer(
        f"""✅ <b>Заявка на вывод создана</b>

Сумма: <b>{wd['amount']}$</b>
Кошелёк: <code>{wd['wallet']}</code>
Статус: <b>pending</b> ⏳

После решения администратора я пришлю сообщение 🙂"""
    )

    # уведомляем админа + кнопки
    try:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Оплачено", callback_data=f"wd_ok:{wd['id']}"),
                    InlineKeyboardButton(text="❌ Отклонить", callback_data=f"wd_no:{wd['id']}"),
                ]
            ]
        )

        uname = message.from_user.username or ""
        uname_line = f"@{uname}" if uname else "—"

        await message.bot.send_message(
            ADMIN_ID,
            f"""📥 <b>Заявка на вывод</b>

ID: <b>#{wd['id']}</b>
Пользователь: <b>{message.from_user.full_name}</b>
Username: {uname_line}
TG ID: <code>{tg_id}</code>
Сумма: <b>{wd['amount']}$</b>
Кошелёк: <code>{wd['wallet']}</code>
Статус: <b>pending</b> ⏳""",
            reply_markup=kb,
        )
    except Exception as e:
        logger.exception("Не удалось уведомить админа о выводе: %s", e)

@router.message()
async def fallback(message: Message):
    if is_spam(message.from_user.id):
        return
    await message.answer(
        "🤔 Я тебя не понял.\n\n"
        "Используй кнопки снизу: <b>Обучение</b>, <b>Подписка</b>, <b>Профиль</b>.\n"
        "Или нажми /start, чтобы перезагрузить меню.",
        reply_markup=main_kb(),
    )

# ---------------------------------------------------------------------------
# START
# ---------------------------------------------------------------------------

async def main():
    if BOT_TOKEN == "PASTE_BOT_TOKEN_HERE" or not BOT_TOKEN:
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

    # Фоновая проверка подписок: напоминания + кик по окончанию
    asyncio.create_task(subscription_watcher(bot))

    await bot.delete_webhook(drop_pending_updates=True)

    await dp.start_polling(
        bot,
        allowed_updates=dp.resolve_used_update_types(),
        polling_timeout=30,
        request_timeout=65,
    )

if __name__ == "__main__":
    asyncio.run(main())
