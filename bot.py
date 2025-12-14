import logging
import sqlite3
import asyncio
import random
import os
import csv
import io
import re
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple, Sequence

import aiohttp
from aiogram import Bot, Dispatcher, executor, types
from auto_signals import auto_signals_worker, build_auto_signal_text, COINGECKO_IDS, QUIET_HOURS_ENABLED, QUIET_HOURS_START, QUIET_HOURS_END, QUIET_HOURS_UTC_OFFSET
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    InputFile,
)

from aiogram.contrib.middlewares.logging import LoggingMiddleware

# ---------------------------------------------------------------------------
# НАСТРОЙКИ      
# ---------------------------------------------------------------------------

# TODO: если хочешь, можешь вернуть сюда чтение из .env, но по твоей просьбе — вставляю сразу константой
BOT_TOKEN = "8306701860:AAFKZXLryFfy7reYYqvE0U5V-Npnr0tU2Oc"

# твой админ ID (из прошлых файлов)
ADMIN_ID = 8585550939

# Tron / TronGrid
# TODO: сюда вставь свой ключ TronGrid, который ты мне отправлял (GUID вида xxxx-xxxx-xxxx)
TRONGRID_API_KEY = "b33b8d65-10c9-4f7b-99e0-ab47f3bbb60f"

# TODO: сюда вставь свой TRON-кошелёк, на который люди отправляют USDT (TRC20)
WALLET_ADDRESS = "TMVnoYkCsU3XHV28P5vMZokcWinqE3pUcK"

# Стандартный контракт USDT TRC20 (можно не менять)
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

# ID канала с сигналами (число, например -1001234567890)
# TODO: вставь сюда ID своего канала с сигналами
SIGNALS_CHANNEL_URL = "https://t.me/fjsidjdjjs"

# Ссылка на сигнальный канал (на случай, если удобнее давать ссылку)
SIGNALS_CHANNEL_ID = -1003215636168


# Ссылка на сигнальный канал (для кнопок и сообщений)
SIGNALS_CHANNEL_LINK = "https://t.me/+uScs9-WDtW5hYTIy"  # 👈 сюда реальную ссылку

# Авто-сигналы
AUTO_SIGNALS_ENABLED = True          # если захочешь вырубить — поставишь False
AUTO_SIGNALS_PER_DAY = 5             # примерно сколько сигналов в сутки
AUTO_SIGNALS_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]  # пары для сигналов

# Уведомления о TP/SL/BE:
# False = пишем ТОЛЬКО в канал с сигналами (без пересылок/рассылок в личку)
TP_UPDATES_TO_USERS = False


# Ссылки на обучающие каналы
TRADING_EDU_CHANNEL = "https://t.me/+RPev0hkFwjk5MmQy"
TRAFFIC_EDU_CHANNEL = "https://t.me/+AA8Un3DxezdkNWQy"

# Контакт поддержки
SUPPORT_CONTACT = "@TradeX_Partner_helper"  # при желании поменяешь на свой @ник

# Цены и проценты
PRICE_PACKAGE = Decimal("100")   # полный доступ
PRICE_RENEWAL = Decimal("50")    # продление сигналов
LEVEL1_PERCENT = Decimal("0.5")  # 50%
LEVEL2_PERCENT = Decimal("0.1")  # 10%

DB_PATH = os.getenv("DB_PATH", "database.db")

# Антиспам (минимальный интервал между сообщениями)
ANTISPAM_SECONDS = 1.2

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PROD: уведомления админу + кулдауны на тяжёлых действиях
# ---------------------------------------------------------------------------

_admin_notify_last: Dict[str, datetime] = {}
_cooldowns: Dict[tuple, datetime] = {}

def _cooldown_remaining(user_id: int, key: str, seconds: int) -> int:
    """Возвращает оставшиеся секунды кулдауна (0 если можно)."""
    now = datetime.utcnow()
    k = (int(user_id), str(key))
    last = _cooldowns.get(k)
    if last is None:
        _cooldowns[k] = now
        return 0
    diff = (now - last).total_seconds()
    if diff >= seconds:
        _cooldowns[k] = now
        return 0
    return int(seconds - diff) + 1

async def notify_admin(text: str, key: str = "generic", cooldown: int = 300) -> None:
    """Шлём админу только иногда (чтобы не спамить)."""
    try:
        now = datetime.utcnow()
        last = _admin_notify_last.get(key)
        if last and (now - last).total_seconds() < cooldown:
            return
        _admin_notify_last[key] = now
        await bot.send_message(ADMIN_ID, text, disable_web_page_preview=True)
    except Exception:
        # Если вдруг нельзя отправить (бот без прав/админ недоступен) — просто молчим
        return

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

    # Пользователи
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
            reg_date TEXT,
            full_access INTEGER DEFAULT 0,   -- 0/1, полный пакет за 100$
            is_blocked INTEGER DEFAULT 0     -- 0/1, блокировка
        )
        """
    )

    # Покупки (пакет / продления)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            product_code TEXT,              -- "package" / "renewal"
            amount REAL,
            status TEXT,                    -- "pending" / "paid"
            created_at TEXT,
            paid_at TEXT,
            tx_id TEXT
        )
        """
    )

    # Ручные заявки на подтверждение оплаты (TXID)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS manual_pay_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            purchase_id INTEGER,
            tg_user_id INTEGER,
            tx_id TEXT,
            status TEXT,          -- 'pending' / 'approved' / 'rejected'
            created_at TEXT,
            processed_at TEXT
        )
        """
    )

    # Подписка на сигналы
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS signals_access (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE,      -- ссылка на users.id
            active_until TEXT            -- UTC datetime (YYYY-mm-dd HH:MM:SS)
        )
        """
    )

    
    # Сигналы (для авто-отслеживания TP/SL)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS signal_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_message_id INTEGER UNIQUE,
            symbol TEXT,
            direction TEXT,               -- 'LONG' / 'SHORT'
            entry_low REAL,
            entry_high REAL,
            sl REAL,
            tp1 REAL,
            tp2 REAL,
            status TEXT DEFAULT 'pending',   -- 'pending' / 'active' / 'closed'
            tp1_hit INTEGER DEFAULT 0,
            tp2_hit INTEGER DEFAULT 0,
            sl_hit INTEGER DEFAULT 0,
            created_at TEXT,
            activated_at TEXT,
            closed_at TEXT,
            last_price REAL,
            last_checked_at TEXT
        )
        """
    )

    # Прогресс по курсам (отдельно трейдинг и трафик)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            course TEXT,                -- "crypto" / "traffic"
            module_index INTEGER,
            UNIQUE (user_id, course)
        )
        """
    )
        # Заявки на вывод партнёрского вознаграждения
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            status TEXT,          -- 'pending', 'done', 'rejected'
            created_at TEXT,
            processed_at TEXT
        )
        """
    )   

    conn.commit()
    conn.close()
    


def get_or_create_user(message: types.Message, referrer_id_db: int = None) -> int:
    user_id = message.from_user.id
    username = message.from_user.username or ""
    first_name = message.from_user.first_name or ""

    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM users WHERE user_id = ?",
        (user_id,),
    )
    row = cur.fetchone()

    if row:
        user_db_id = row[0]
        # на всякий случай обновляем логин / имя
        cur.execute(
            "UPDATE users SET username = ?, first_name = ? WHERE id = ?",
            (username, first_name, user_db_id),
        )
        conn.commit()
        conn.close()
        return user_db_id

    reg_date = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        """
        INSERT INTO users (user_id, username, first_name, referrer_id, reg_date)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, username, first_name, referrer_id_db, reg_date),
    )
    conn.commit()
    user_db_id = cur.lastrowid
    conn.close()
    return user_db_id


def get_user_by_tg(user_id: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, user_id, username, first_name,
               referrer_id, balance, total_earned, full_access
        FROM users WHERE user_id = ?
        """,
        (user_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def set_full_access(user_db_id: int, value: bool = True):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET full_access = ? WHERE id = ?",
        (1 if value else 0, user_db_id),
    )
    conn.commit()
    conn.close()


def has_full_access(user_db_id: int) -> bool:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT full_access FROM users WHERE id = ?", (user_db_id,))
    row = cur.fetchone()
    conn.close()
    return bool(row and row[0])


def create_purchase(user_db_id: int, product_code: str, base_price: Decimal) -> int:
    """
    Создаём покупку с уникальным хвостом (например 100.543).
    """
    # уникальный хвост до 0.999
    tail = Decimal(random.randint(1, 999)) / Decimal("1000")
    amount = (base_price + tail).quantize(Decimal("0.000"), rounding=ROUND_DOWN)

    conn = db_connect()
    cur = conn.cursor()
    created_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        """
        INSERT INTO purchases (user_id, product_code, amount, status, created_at)
        VALUES (?, ?, ?, 'pending', ?)
        """,
        (user_db_id, product_code, float(amount), created_at),
    )
    conn.commit()
    purchase_id = cur.lastrowid
    conn.close()
    return purchase_id


def get_purchase(purchase_id: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, user_id, product_code, amount, status, created_at, tx_id
        FROM purchases WHERE id = ?
        """,
        (purchase_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def mark_purchase_paid(purchase_id: int, tx_id: str):
    conn = db_connect()
    cur = conn.cursor()
    paid_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        """
        UPDATE purchases
        SET status = 'paid', paid_at = ?, tx_id = ?
        WHERE id = ?
        """,
        (paid_at, tx_id, purchase_id),
    )
    conn.commit()
    conn.close()


def is_txid_used(txid: str) -> bool:
    """Защита от повторного использования одного и того же TXID."""
    if not txid:
        return False
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM purchases WHERE tx_id = ? LIMIT 1", (txid,))
    row = cur.fetchone()
    conn.close()
    return bool(row)


def upsert_manual_pay_request(purchase_id: int, tg_user_id: int, txid: str) -> int:
    """Создаёт или обновляет pending-заявку на ручное подтверждение оплаты."""
    conn = db_connect()
    cur = conn.cursor()
    created_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    cur.execute(
        "SELECT id FROM manual_pay_requests WHERE purchase_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1",
        (purchase_id,),
    )
    row = cur.fetchone()
    if row:
        req_id = int(row[0])
        cur.execute(
            "UPDATE manual_pay_requests SET tx_id = ?, tg_user_id = ?, created_at = ? WHERE id = ?",
            (txid, tg_user_id, created_at, req_id),
        )
        conn.commit()
        conn.close()
        return req_id

    cur.execute(
        """
        INSERT INTO manual_pay_requests (purchase_id, tg_user_id, tx_id, status, created_at)
        VALUES (?, ?, ?, 'pending', ?)
        """,
        (purchase_id, tg_user_id, txid, created_at),
    )
    conn.commit()
    req_id = int(cur.lastrowid)
    conn.close()
    return req_id


def get_manual_pay_request(req_id: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, purchase_id, tg_user_id, tx_id, status, created_at, processed_at FROM manual_pay_requests WHERE id = ?",
        (req_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def set_manual_pay_request_status(req_id: int, status: str):
    conn = db_connect()
    cur = conn.cursor()
    processed_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        "UPDATE manual_pay_requests SET status = ?, processed_at = ? WHERE id = ?",
        (status, processed_at, req_id),
    )
    conn.commit()
    conn.close()

def extend_signals(user_db_id: int, days: int = 30):
    conn = db_connect()
    cur = conn.cursor()
    now = datetime.utcnow()
    cur.execute("SELECT active_until FROM signals_access WHERE user_id = ?", (user_db_id,))
    row = cur.fetchone()
    if row and row[0]:
        current_until = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
        base = max(now, current_until)
    else:
        base = now
    new_until = base + timedelta(days=days)
    new_until_str = new_until.strftime("%Y-%m-%d %H:%M:%S")
    if row:
        cur.execute(
            "UPDATE signals_access SET active_until = ? WHERE user_id = ?",
            (new_until_str, user_db_id),
        )
    else:
        cur.execute(
            "INSERT INTO signals_access (user_id, active_until) VALUES (?, ?)",
            (user_db_id, new_until_str),
        )
    conn.commit()
    conn.close()


def get_signals_until(user_db_id: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT active_until FROM signals_access WHERE user_id = ?", (user_db_id,))
    row = cur.fetchone()
    conn.close()
    if not row or not row[0]:
        return None
    try:
        return datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None



# ---------------------------------------------------------------------------
# АВТО-ОТСЛЕЖИВАНИЕ TP/SL (автоматические посты о закрытии тейков)
# ---------------------------------------------------------------------------

def _strip_tags(s: str) -> str:
    if not s:
        return ""
    # Telegram хранит entities, но на входе у нас может быть HTML-строка.
    return re.sub(r"<[^>]+>", "", s)

def _to_decimal(s: str) -> Optional[Decimal]:
    try:
        return Decimal(s.replace(",", ".").strip())
    except Exception:
        return None

def parse_signal_from_text(text: str) -> Optional[Dict[str, object]]:
    """Парсим текст сигнала (и HTML, и обычный текст) -> параметры сделки."""
    plain = _strip_tags(text)

    # Пара: BTC/USDT
    m = re.search(r"Сигнал\s*по\s*([A-Z0-9]{2,12})\s*/\s*([A-Z0-9]{2,12})", plain)
    if not m:
        return None
    base, quote = m.group(1), m.group(2)
    symbol = f"{base}{quote}".upper()

    # Направление LONG/SHORT
    m = re.search(r"Параметры\s+сделки\s*\((LONG|SHORT)\)", plain, re.IGNORECASE)
    if not m:
        return None
    direction = m.group(1).upper()

    # Вход: 123–456 (допускаем '-' или '–')
    m = re.search(r"Вход:\s*([0-9][0-9\.,]*)\s*[–\-]\s*([0-9][0-9\.,]*)", plain)
    if not m:
        return None
    entry_low = _to_decimal(m.group(1))
    entry_high = _to_decimal(m.group(2))

    m = re.search(r"Стоп-лосс:\s*([0-9][0-9\.,]*)", plain)
    sl = _to_decimal(m.group(1)) if m else None

    m = re.search(r"Тейк-профит\s*1:\s*([0-9][0-9\.,]*)", plain)
    tp1 = _to_decimal(m.group(1)) if m else None

    m = re.search(r"Тейк-профит\s*2:\s*([0-9][0-9\.,]*)", plain)
    tp2 = _to_decimal(m.group(1)) if m else None

    if not (entry_low and entry_high and sl and tp1 and tp2):
        return None

    return {
        "symbol": symbol,
        "base": base,
        "quote": quote,
        "direction": direction,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
    }

def save_signal_trade(channel_message_id: int, text: str) -> bool:
    """Сохраняем сигнал в БД для дальнейшего мониторинга. True если сохранили."""
    data = parse_signal_from_text(text)
    if not data:
        return False

    created_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    conn = db_connect()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT OR IGNORE INTO signal_trades
            (channel_message_id, symbol, direction, entry_low, entry_high, sl, tp1, tp2, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(channel_message_id),
                str(data["symbol"]),
                str(data["direction"]),
                float(data["entry_low"]),
                float(data["entry_high"]),
                float(data["sl"]),
                float(data["tp1"]),
                float(data["tp2"]),
                created_at,
            ),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()

def _get_open_trades() -> List[Tuple]:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, channel_message_id, symbol, direction,
               entry_low, entry_high, sl, tp1, tp2,
               status, tp1_hit, tp2_hit, sl_hit, activated_at
        FROM signal_trades
        WHERE status != 'closed'
        ORDER BY id ASC
        """
    )
    rows = cur.fetchall()
    conn.close()
    return rows or []

def _update_trade_status(trade_id: int, **fields):
    if not fields:
        return
    cols = []
    vals = []
    for k, v in fields.items():
        cols.append(f"{k} = ?")
        vals.append(v)
    vals.append(trade_id)

    conn = db_connect()
    cur = conn.cursor()
    cur.execute(f"UPDATE signal_trades SET {', '.join(cols)} WHERE id = ?", vals)
    conn.commit()
    conn.close()


def _update_trade_status_where(trade_id: int, where_sql: str = "", where_params: tuple = (), **fields) -> bool:
    """Атомарное обновление сделки с дополнительными условиями в WHERE.
    Возвращает True, если строка реально обновилась (чтобы не было дублей уведомлений)."""
    if not fields:
        return False
    cols = []
    vals = []
    for k, v in fields.items():
        cols.append(f"{k} = ?")
        vals.append(v)
    # id — первый параметр WHERE, затем дополнительные
    vals.append(trade_id)
    vals.extend(list(where_params))

    conn = db_connect()
    cur = conn.cursor()
    try:
        cur.execute(f"UPDATE signal_trades SET {', '.join(cols)} WHERE id = ? {where_sql}", vals)
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()

def get_active_signals_tg_ids() -> List[int]:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT u.user_id
        FROM signals_access sa
        JOIN users u ON sa.user_id = u.id
        WHERE sa.active_until IS NOT NULL AND sa.active_until > ?
        """,
        (now,),
    )
    rows = cur.fetchall()
    conn.close()
    return [int(r[0]) for r in rows if r and r[0] is not None]

async def broadcast_to_active_signals(text: str, kb: Optional[InlineKeyboardMarkup] = None):
    for tg_id in get_active_signals_tg_ids():
        try:
            await bot.send_message(tg_id, text, reply_markup=kb, disable_web_page_preview=True)
        except Exception:
            pass
        await asyncio.sleep(0.05)

async def _fetch_binance_price(session: aiohttp.ClientSession, symbol: str) -> Optional[Decimal]:
    url = "https://api.binance.com/api/v3/ticker/price"
    try:
        async with session.get(url, params={"symbol": symbol}, timeout=10) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            p = data.get("price")
            return _to_decimal(str(p)) if p is not None else None
    except Exception:
        return None

async def _fetch_coingecko_price(session: aiohttp.ClientSession, symbol: str) -> Optional[Decimal]:
    # CoinGecko отдаёт USD, для USDT это почти то же.
    coin_id = COINGECKO_IDS.get(symbol)
    if not coin_id:
        return None
    url = "https://api.coingecko.com/api/v3/simple/price"
    try:
        async with session.get(url, params={"ids": coin_id, "vs_currencies": "usd"}, timeout=10) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            usd = (data.get(coin_id) or {}).get("usd")
            return _to_decimal(str(usd)) if usd is not None else None
    except Exception:
        return None

async def fetch_price(session: aiohttp.ClientSession, symbol: str) -> Optional[Decimal]:
    p = await _fetch_binance_price(session, symbol)
    if p is not None:
        return p
    return await _fetch_coingecko_price(session, symbol)

def _fmt_pct(x: Decimal) -> str:
    try:
        return str(x.quantize(Decimal("0.01")))
    except Exception:
        return str(x)


def _fmt_price(p: Decimal) -> str:
    """Формат цены с разумным количеством знаков."""
    try:
        if p >= Decimal("100"):
            q = p.quantize(Decimal("0.1"))
        elif p >= Decimal("1"):
            q = p.quantize(Decimal("0.01"))
        elif p >= Decimal("0.1"):
            q = p.quantize(Decimal("0.001"))
        else:
            q = p.quantize(Decimal("0.0001"))
        return str(q)
    except Exception:
        return str(p)

async def _post_trade_update(channel_message_id: int, text: str):
    # Постим обновление ТОЛЬКО в канал (ответом на исходный сигнал)
    try:
        await bot.send_message(
            SIGNALS_CHANNEL_ID,
            text,
            reply_to_message_id=channel_message_id,
            disable_web_page_preview=True,
        )
    except Exception:
        try:
            await bot.send_message(SIGNALS_CHANNEL_ID, text, disable_web_page_preview=True)
        except Exception:
            pass

    # Если захочешь дублировать в личку подписчикам — включи флаг TP_UPDATES_TO_USERS
    if TP_UPDATES_TO_USERS:
        await broadcast_to_active_signals(text)

async def tp_monitor_worker():
    """Фоновый воркер: следит за активными сигналами и сам пишет про TP/SL."""
    await asyncio.sleep(10)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                trades = _get_open_trades()
                if not trades:
                    await asyncio.sleep(20)
                    continue

                # цены получаем по уникальным символам
                symbols = sorted({t[2] for t in trades if t[2]})
                prices: Dict[str, Decimal] = {}
                for sym in symbols:
                    p = await fetch_price(session, sym)
                    if p is not None:
                        prices[sym] = p
                    await asyncio.sleep(0.05)

                now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

                for (
                    trade_id, msg_id, symbol, direction,
                    entry_low_f, entry_high_f, sl_f, tp1_f, tp2_f,
                    status, tp1_hit, tp2_hit, sl_hit, activated_at
                ) in trades:

                    price = prices.get(symbol)
                    if price is None:
                        _update_trade_status(trade_id, last_checked_at=now_str)
                        continue

                    entry_low = Decimal(str(entry_low_f))
                    entry_high = Decimal(str(entry_high_f))
                    sl = Decimal(str(sl_f))
                    tp1 = Decimal(str(tp1_f))
                    tp2 = Decimal(str(tp2_f))
                    dir_u = (direction or "").upper()

                    # pending -> active, когда цена вошла в зону входа
                    if status == "pending":
                        if entry_low <= price <= entry_high:
                            _update_trade_status(
                                trade_id,
                                status="active",
                                activated_at=now_str,
                                last_price=float(price),
                                last_checked_at=now_str,
                            )
                        else:
                            _update_trade_status(trade_id, last_price=float(price), last_checked_at=now_str)
                        continue

                    if status == "closed":
                        continue

                    entry_mid = (entry_low + entry_high) / Decimal("2")

                    def profit_pct(target: Decimal) -> Decimal:
                        if dir_u == "LONG":
                            return (target - entry_mid) / entry_mid * Decimal("100")
                        return (entry_mid - target) / entry_mid * Decimal("100")

                    tp1_hit_b = bool(tp1_hit)
                    tp2_hit_b = bool(tp2_hit)
                    sl_hit_b = bool(sl_hit)

                    # СТОП
                    sl_trigger = (price <= sl) if dir_u == "LONG" else (price >= sl)
                    if (not sl_hit_b) and sl_trigger:
                        # Если после TP1 мы перенесли SL в безубыток (SL ≈ entry_mid) — закрываем как BE
                        be_threshold = Decimal("0.0005")  # 0.05% допуска из-за округлений
                        is_be = tp1_hit_b and (abs(sl - entry_mid) / entry_mid <= be_threshold)

                        if is_be:
                            text = (
                                f"🔒 <b>Безубыток</b> ({symbol})\n"
                                f"Цена вернулась к входу — сделка закрыта в <b>{_fmt_pct(Decimal('0'))}%</b>\n"
                                f"Цена: <b>{_fmt_price(price)}</b>\n"
                                f"Вход (BE): <b>{_fmt_price(entry_mid)}</b>"
                            )
                        else:
                            pct = (sl - entry_mid) / entry_mid * Decimal("100") if dir_u == "LONG" else (entry_mid - sl) / entry_mid * Decimal("100")
                            text = (
                                f"🛑 <b>Стоп-лосс сработал</b> ({symbol})\n"
                                f"Цена: <b>{_fmt_price(price)}</b>\n"
                                f"Результат от входа: <b>{_fmt_pct(pct)}%</b>"
                            )

                        if _update_trade_status_where(
                            trade_id,
                            "AND sl_hit = 0 AND status != 'closed'",
                            sl_hit=1,
                            status="closed",
                            closed_at=now_str,
                            last_price=float(price),
                            last_checked_at=now_str,
                        ):
                            await _post_trade_update(int(msg_id), text)
                        continue

                    # TP1
                    tp1_trigger = (price >= tp1) if dir_u == "LONG" else (price <= tp1)
                    if (not tp1_hit_b) and tp1_trigger:
                        pct = profit_pct(tp1)

                        # Переводим стоп в безубыток после TP1 (по середине зоны входа)
                        be_price = entry_mid

                        text = (
                            f"🎯 <b>TP1 закрыт</b> ✅ ({symbol})\n"
                            f"Цена: <b>{_fmt_price(price)}</b>\n"
                            f"Профит от входа: <b>+{_fmt_pct(pct)}%</b>\n"
                            f"🔒 Стоп перенесён в <b>безубыток</b>: <b>{_fmt_price(be_price)}</b>\n"
                            f"Держим дальше до TP2 💎"
                        )
                        if _update_trade_status_where(
                            trade_id,
                            "AND tp1_hit = 0 AND status != 'closed'",
                            tp1_hit=1,
                            sl=float(be_price),
                            last_price=float(price),
                            last_checked_at=now_str,
                        ):
                            await _post_trade_update(int(msg_id), text)

                    # TP2 (финал)
                    tp2_trigger = (price >= tp2) if dir_u == "LONG" else (price <= tp2)
                    if (not tp2_hit_b) and tp2_trigger:
                        pct = profit_pct(tp2)
                        text = (
                            f"🏁 <b>TP2 закрыт</b> ✅ ({symbol})\n"
                            f"Цена: <b>{_fmt_price(price)}</b>\n"
                            f"Профит от входа: <b>+{_fmt_pct(pct)}%</b>\n"
                            f"Сделка закрыта полностью 🎉"
                        )
                        if _update_trade_status_where(
                            trade_id,
                            "AND tp2_hit = 0 AND status != 'closed'",
                            tp2_hit=1,
                            status="closed",
                            closed_at=now_str,
                            last_price=float(price),
                            last_checked_at=now_str,
                        ):
                            await _post_trade_update(int(msg_id), text)
                        continue

                    _update_trade_status(trade_id, last_price=float(price), last_checked_at=now_str)

            except Exception as e:
                logger.exception("tp_monitor_worker error: %s", e)
                await notify_admin(f"🚨 tp_monitor_worker error: {e}", key="tp_monitor", cooldown=600)

            await asyncio.sleep(20)

async def auto_signals_worker_tracked(
    bot: Bot,
    signals_channel_id: int,
    auto_signals_per_day: int,
    symbols: Sequence[str],
    enabled: bool,
) -> None:
    """Как auto_signals_worker, но с сохранением сигнала в БД для TP/SL."""
    if not enabled:
        logger.info("Auto signals disabled, worker not started.")
        return
    if not isinstance(signals_channel_id, int):
        logger.warning("signals_channel_id is not int, auto-signals disabled.")
        return

    interval = int(24 * 3600 / max(auto_signals_per_day, 1))
    await asyncio.sleep(15)

    while True:
        try:
            now_utc = datetime.utcnow()
            local_hour = (now_utc.hour + QUIET_HOURS_UTC_OFFSET) % 24

            in_quiet = False
            if QUIET_HOURS_ENABLED:
                if QUIET_HOURS_START <= QUIET_HOURS_END:
                    in_quiet = QUIET_HOURS_START <= local_hour < QUIET_HOURS_END
                else:
                    in_quiet = local_hour >= QUIET_HOURS_START or local_hour < QUIET_HOURS_END

            if not in_quiet:
                text = await build_auto_signal_text(symbols, enabled)
                if text:
                    msg = await bot.send_message(signals_channel_id, text)
                    save_signal_trade(msg.message_id, text)
                    logger.info("Auto signal sent+saved (msg_id=%s).", msg.message_id)
            else:
                logger.info("Auto signal skipped due to quiet hours (local hour=%s)", local_hour)
        except Exception as e:
            logger.error("Auto signals tracked worker error: %s", e)
            await notify_admin(f"⚠️ Auto-signals worker error: {e}", key="auto_signals_worker", cooldown=600)

        await asyncio.sleep(interval)

def add_balance(user_db_id: int, amount: Decimal):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE users
        SET balance = balance + ?, total_earned = total_earned + ?
        WHERE id = ?
        """,
        (float(amount), float(amount), user_db_id),
    )
    conn.commit()
    conn.close()


def get_referrer_chain(user_db_id: int):
    """
    id первого и второго уровня (в таблице users)
    """
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT referrer_id FROM users WHERE id = ?", (user_db_id,))
    row = cur.fetchone()
    lvl1_id = row[0] if row else None

    lvl2_id = None
    if lvl1_id:
        cur.execute("SELECT referrer_id FROM users WHERE id = ?", (lvl1_id,))
        row2 = cur.fetchone()
        lvl2_id = row2[0] if row2 else None

    conn.close()
    return lvl1_id, lvl2_id


def create_withdraw_request(user_db_id: int, amount: Decimal):
    """
    Создаём заявку на вывод партнёрского вознаграждения.
    """
    conn = db_connect()
    cur = conn.cursor()
    created_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        """
        INSERT INTO withdrawals (user_id, amount, status, created_at)
        VALUES (?, ?, 'pending', ?)
        """,
        (user_db_id, float(amount), created_at),
    )
    conn.commit()
    conn.close()


def get_pending_withdraw(user_db_id: int):
    """
    Возвращаем последнюю необработанную заявку ('pending') для пользователя
    или None, если её нет.
    """
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, amount, status, created_at
        FROM withdrawals
        WHERE user_id = ? AND status = 'pending'
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_db_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row



def save_progress(user_db_id: int, course: str, module_index: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO progress (user_id, course, module_index)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id, course) DO UPDATE SET module_index = excluded.module_index
        """,
        (user_db_id, course, module_index),
    )
    conn.commit()
    conn.close()


def get_progress(user_db_id: int, course: str) -> int:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT module_index FROM progress WHERE user_id = ? AND course = ?",
        (user_db_id, course),
    )
    row = cur.fetchone()
    conn.close()
    return row[0] if row else -1


def count_referrals(user_db_id: int):
    conn = db_connect()
    cur = conn.cursor()
    # 1 линия
    cur.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (user_db_id,))
    lvl1 = cur.fetchone()[0]
    # 2 линия
    cur.execute(
        """
        SELECT COUNT(*)
        FROM users
        WHERE referrer_id IN (
            SELECT id FROM users WHERE referrer_id = ?
        )
        """,
        (user_db_id,),
    )
    lvl2 = cur.fetchone()[0]
    conn.close()
    return lvl1, lvl2


# ---------------------------------------------------------------------------
# АНТИСПАМ
# ---------------------------------------------------------------------------

user_last_action = {}

# Ручная проверка оплат (fallback): ждём TXID от пользователя после нажатия кнопки
MANUAL_TX_WAIT: Dict[int, int] = {}  # tg_user_id -> purchase_id


def is_spam(user_id: int) -> bool:
    now = datetime.utcnow()
    last = user_last_action.get(user_id)
    user_last_action[user_id] = now
    if not last:
        return False
    return (now - last) < timedelta(seconds=ANTISPAM_SECONDS)


# ---------------------------------------------------------------------------
# ТРАНЗАКЦИИ TRONGRID
# ---------------------------------------------------------------------------


async def fetch_trc20_transactions() -> list:
    """
    Получаем последние TRC20-транзакции по нашему кошельку.
    """
    headers = {"TRON-PRO-API-KEY": TRONGRID_API_KEY} if TRONGRID_API_KEY else {}
    url = f"https://api.trongrid.io/v1/accounts/{WALLET_ADDRESS}/transactions/trc20"
    params = {
        "limit": 50,
        "contract_address": USDT_CONTRACT,
        "only_confirmed": "true",
    }

    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, params=params, timeout=20) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error("TronGrid error %s: %s", resp.status, text)
                    await notify_admin(f"⚠️ TronGrid ответил {resp.status}. Проверка оплат может временно не работать.", key="trongrid_http", cooldown=600)
                    return []
                data = await resp.json()
                return data.get("data", [])
    except Exception as e:
        logger.exception("TronGrid request failed: %s", e)
        await notify_admin(f"🚨 TronGrid request failed: {e}", key="trongrid_exc", cooldown=600)
        return []



async def find_payment_for_purchase(amount: Decimal, created_at: datetime) -> str | None:
    """
    Ищем транзакцию по сумме (с хвостиком) и времени создания.
    Возвращаем tx_id или None.
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

            # чуть-чуть допускаем плавающую точку
            if abs(value - amount) > Decimal("0.0005"):
                continue

            ts_ms = tx.get("block_timestamp")
            tx_time = datetime.utcfromtimestamp(ts_ms / 1000.0)

            # проверяем, что платёж не сильно старше заявки (например, не старше 24 часов)
            if tx_time + timedelta(hours=24) < created_at:
                continue

            tx_id = tx.get("transaction_id")
            return tx_id
        except Exception as e:
            logger.exception("Error while parsing Tron tx: %s", e)
            continue

    return None


async def process_successful_payment(purchase_row):
    """
    purchase_row: (id, user_id, product_code, amount, status, created_at, tx_id)
    Начисляет доступ, продление, партнёрку.
    """
    purchase_id, user_db_id, product_code, amount_f, status, created_at_str, _ = purchase_row
    amount = Decimal(str(amount_f))

    # Помечаем как оплачено (tx_id уже определили до вызова)
    # tx_id мы передадим из проверяющей функции
    # Здесь только логика начислений

    # Если это пакет за 100$
    if product_code == "package":
        # открываем полный доступ
        set_full_access(user_db_id, True)
        # продлеваем сигналы на месяц
        extend_signals(user_db_id, days=30)

        # реферальные начисления считаем от базовой цены (100$), а не от суммы с хвостом
        base = PRICE_PACKAGE
        lvl1_id, lvl2_id = get_referrer_chain(user_db_id)
        lvl1_bonus = (base * LEVEL1_PERCENT).quantize(Decimal("0.01"))
        lvl2_bonus = (base * LEVEL2_PERCENT).quantize(Decimal("0.01"))

        # 1 уровень
        if lvl1_id:
            add_balance(lvl1_id, lvl1_bonus)
            # уведомление
            conn = db_connect()
            cur = conn.cursor()
            cur.execute("SELECT user_id FROM users WHERE id = ?", (lvl1_id,))
            r = cur.fetchone()
            conn.close()
            if r:
                try:
                    await bot.send_message(
                        r[0],
                        f"💰 <b>Начислено {lvl1_bonus}$</b> за личную рекомендацию.\n"
                        f"Твой партнёр совершил покупку полного доступа.",
                    )
                except Exception:
                    pass

        # 2 уровень
        if lvl2_id:
            add_balance(lvl2_id, lvl2_bonus)
            conn = db_connect()
            cur = conn.cursor()
            cur.execute("SELECT user_id FROM users WHERE id = ?", (lvl2_id,))
            r = cur.fetchone()
            conn.close()
            if r:
                try:
                    await bot.send_message(
                        r[0],
                        f"💸 <b>Начислено {lvl2_bonus}$</b> со второго уровня.\n"
                        f"Партнёр второй линии купил полный доступ.",
                    )
                except Exception:
                    pass

        # уведомляем покупателя
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE id = ?", (user_db_id,))
        r = cur.fetchone()
        conn.close()
        if r:
            tg_id = r[0]
            try:
                await bot.send_message(
                    tg_id,
                    "✅ <b>Оплата подтверждена!</b>\n\n"
                    "Полный доступ к обучению, партнёрке и сигналам (на 1 месяц) открыт.\n"
                    f"Сигналы приходят в канале: {SIGNALS_CHANNEL_LINK}",
                )
            except Exception:
                pass

    elif product_code == "renewal":
        # только продление сигналов, без партнёрки
        extend_signals(user_db_id, days=30)
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE id = ?", (user_db_id,))
        r = cur.fetchone()
        conn.close()
        if r:
            tg_id = r[0]
            try:
                await bot.send_message(
                    tg_id,
                    "✅ <b>Продление сигналов оплачено!</b>\n\n"
                    "Подписка на сигнальный канал продлена ещё на 30 дней.",
                )
            except Exception:
                pass


# ---------------------------------------------------------------------------
# КУРСЫ (8 блоков трейдинг, 6 блоков трафик)
# ---------------------------------------------------------------------------

# Для экономии места делаю по одному большому тексту на модуль.
# При желании потом расширишь каждый блок на несколько уроков.

COURSE_CRYPTO = [
    (
        "1️⃣ Модуль 1. Базовая подготовка и безопасность",
        "🧠 <b>Модуль 1. Базовая подготовка и безопасность</b>\n\n"
        "В этом модуле мы не лезем в сложные стратегии. Твоя задача — понять, что ты делаешь и где именно "
        "находятся основные риски.\n\n"
        "Что разберём:\n"
        "• чем трейдинг отличается от казино и инвестиций\n"
        "• какие типы бирж и аккаунтов бывают\n"
        "• базовые настройки безопасности (2FA, пароли, антифишинг-коды)\n"
        "• почему нельзя торговать с «последних денег»\n\n"
        "Задача модуля — сформировать у тебя здоровое отношение к рынку: без иллюзий «кнопки бабло», "
        "но и без драматизации.\n\n"
        "<b>Домашка:</b> подключи двухфакторную аутентификацию на бирже, сделай отдельную почту под трейдинг "
        "и пропиши для себя правило: какую сумму ты готов потерять без боли (это и есть твой риск-капитал).",
    ),
    (
        "2️⃣ Модуль 2. Как устроен рынок и графики",
        "📊 <b>Модуль 2. Как устроен рынок и графики</b>\n\n"
        "Здесь мы разбираем, что вообще происходит на графике и откуда берутся свечи.\n\n"
        "Разберём:\n"
        "• что такое ордера, стакан, ликвидность\n"
        "• виды графиков: свечные, линейные, Heikin Ashi\n"
        "• таймфреймы и почему «торговать всё подряд» — путь в никуда\n"
        "• кто такие маркетмейкеры и почему они двигают рынок\n\n"
        "После модуля ты перестанешь видеть в графике «хаос» — появится ощущение структуры.\n\n"
        "<b>Домашка:</b> выбери одну биржу и один инструмент (например BTC/USDT). "
        "Понаблюдай за ним на разных таймфреймах (M5, M15, H1, H4), отметь, как меняется скорость движения.",
    ),
    (
        "3️⃣ Модуль 3. Психология трейдинга и типичные ошибки",
        "🧩 <b>Модуль 3. Психология трейдинга</b>\n\n"
        "90% людей сливают депозит не потому, что не знают стратегий, а потому, что нарушают свои же правила.\n\n"
        "Разберём:\n"
        "• FOMO (страх упустить движение) и как он толкает входить в конце тренда\n"
        "• revenge-trading — попытка «отбиться» после минуса\n"
        "• эффект серии — почему после 3 плюсов подряд хочется «нажать побольше»\n"
        "• как сформировать рабочий дневник трейдера\n\n"
        "Ты поймёшь, что эмоции — это не слабость, а сигнал. Важно научиться их распознавать и "
        "останавливаться, когда тебя «ведёт».\n\n"
        "<b>Домашка:</b> заведите табличку/док, куда будешь записывать каждую сделку: дата, инструмент, вход, выход, "
        "стоп, риск, эмоции до/после сделки. Это база для роста.",
    ),
    (
        "4️⃣ Модуль 4. Риск-менеджмент и размер позиции",
        "⚖️ <b>Модуль 4. Риск-менеджмент</b>\n\n"
        "Если ты не контролируешь риск — рынок сделает это за тебя, но жестко.\n\n"
        "Разберём:\n"
        "• правило 1–2% риска на сделку\n"
        "• как считать объём позиции под заданный стоп\n"
        "• почему усреднение против тренда чаще всего ведёт к сливу\n"
        "• как пережить серию убыточных сделок без уничтожения депозита\n\n"
        "Мы переведём риск из «страха потерять» в чёткую формулу.\n\n"
        "<b>Домашка:</b> возьми свой текущий депозит и посчитай, какой максимальный размер позиции у тебя должен быть "
        "при стопе 3%, 5% и 8% при риске 1% от депозита.",
    ),
    (
        "5️⃣ Модуль 5. Базовая трендовая стратегия",
        "📈 <b>Модуль 5. Базовая трендовая стратегия</b>\n\n"
        "Вместо ловли разворотов мы работаем по тренду — это проще и статистически выгоднее.\n\n"
        "Разберём:\n"
        "• как определять тренд по структуре максимумов и минимумов\n"
        "• что такое импульс и коррекция\n"
        "• базовая логика входа «по тренду после отката»\n"
        "• куда ставить стоп и как фиксировать прибыль частями\n\n"
        "<b>Домашка:</b> найди на графике 10 ситуаций, где тренд уже очевидно сформирован, и отметь, "
        "где логично было бы войти по тренду после коррекции. Это тренировка зрения.",
    ),
    (
        "6️⃣ Модуль 6. Работа с уровнями и зонами ликвидности",
        "🧱 <b>Модуль 6. Уровни и ликвидность</b>\n\n"
        "Здесь мы добавляем к тренду уровни, от которых цена часто реагирует.\n\n"
        "Разберём:\n"
        "• как отмечать значимые уровни на старших таймфреймах\n"
        "• почему «каждый пик — уровень» не работает\n"
        "• что такое зоны стопов и как крупные игроки их используют\n"
        "• как совмещать уровни с трендом и получать более сильные точки входа\n\n"
        "<b>Домашка:</b> на своём основном инструменте отметь 5–7 ключевых зон, где цена сильно реагировала "
        "за последние месяцы, и посмотри, как там шла борьба покупателей и продавцов.",
    ),
    (
        "7️⃣ Модуль 7. Пошаговый план торговли",
        "📋 <b>Модуль 7. План торговли</b>\n\n"
        "Без плана ты всегда будешь торговать эмоциями. Здесь собираем систему воедино.\n\n"
        "Разберём:\n"
        "• чек-лист перед входом в сделку\n"
        "• пример готового плана: от поиска инструмента до выхода из позиции\n"
        "• как встроить в план риск-менеджмент и лимит по убытку в день\n"
        "• как проверять свои сделки раз в неделю и корректировать стратегию\n\n"
        "<b>Домашка:</b> напиши свой чек-лист на 5–10 пунктов, который ты будешь прогонять перед каждой сделкой. "
        "И прикрепи его куда-нибудь на видное место.",
    ),
    (
        "8️⃣ Модуль 8. Практика и переход на реальные деньги",
        "🚀 <b>Модуль 8. Практика и переход на реальные деньги</b>\n\n"
        "Финальный модуль — про то, как аккуратно перейти от теории и демо к реальным деньгам.\n\n"
        "Разберём:\n"
        "• как тестировать стратегию на истории и в демо-режиме\n"
        "• как переходить на реальные деньги маленькими шагами\n"
        "• как фиксировать результат не только в долларах, но и в качестве исполнения плана\n"
        "• что делать, если после перехода на реал всё «ломается» психологически\n\n"
        "<b>Домашка:</b> составь план перехода на реал на 1–3 месяца: "
        "какой объём, сколько сделок, какие критерии «я готов увеличить размер позиции».",
    ),
]

COURSE_TRAFFIC = [
    (
        "1️⃣ Модуль 1. Основы арбитража и воронки",
        "🚀 <b>Модуль 1. Основы арбитража и воронки</b>\n\n"
        "Разберём, как вообще устроен арбитраж и перелив трафика в деньгах.\n\n"
        "• что такое оффер, KPI и payout\n"
        "• какие вертикали существуют (финансы, нутра, гейминг, сабскрипшены и т.д.)\n"
        "• чем отличается холодный трафик от тёплого\n"
        "• зачем тебе вообще Telegram как финальная точка воронки\n\n"
        "<b>Домашка:</b> выпиши 3–5 ниш, которые тебе интересны, и найди по ним офферы "
        "в открытых партнёрках (без углубления, просто чтобы увидеть, как это выглядит.",
    ),
    (
        "2️⃣ Модуль 2. Источники трафика и выбор стартовой площадки",
        "🌐 <b>Модуль 2. Источники трафика</b>\n\n"
        "Ты не обязан запускаться во всех источниках. На старте достаточно выбрать 1–2.\n\n"
        "Разберём:\n"
        "• TikTok, Reels, Shorts как источник бесплатного/дешёвого трафика\n"
        "• плюсы и минусы платного трафика (Facebook, TikTok Ads, myTarget и т.д.)\n"
        "• как выбрать источник под свой бюджет и уровень опыта\n"
        "• примеры рабочих связок «ролики → бот → оффер/подписка»\n\n"
        "<b>Домашка:</b> выбери один основной источник трафика и один запасной. "
        "Запиши, почему именно они и какие ограничения там есть (модерация, креативы и т.п.).",
    ),
    (
        "3️⃣ Модуль 3. Контент и креативы под перелив в бот",
        "🎨 <b>Модуль 3. Контент и креативы</b>\n\n"
        "Тебе не нужно изобретать шедевры. Важно делать понятный, повторяемый контент.\n\n"
        "Разберём:\n"
        "• как делать ролики, которые приводят трафик в Telegram, а не просто набирают просмотры\n"
        "• структура ролика: зацепка → ценность → переход в бот\n"
        "• простые форматы для ниши крипты/дохода: разбор сделок, мини-обучение, кейсы, истории\n"
        "• как адаптировать чужие идеи легально (без копипаста)\n\n"
        "<b>Домашка:</b> придумай 10 тем для коротких роликов, которые логично ведут в твой бот. "
        "Напиши к ним примерный сценарий на 3–5 строк.",
    ),
    (
        "4️⃣ Модуль 4. Трафик → Бот → Монетизация",
        "🔁 <b>Модуль 4. Воронка: трафик → бот → деньги</b>\n\n"
        "Разберём связку на примере нашего бота.\n\n"
        "• точка входа: ролик/объявление → ссылка на бота\n"
        "• приветственное сообщение и первый экран (как ты видел в этом проекте)\n"
        "• куда вести человека дальше: обучение, заработок, профиль\n"
        "• где происходит монетизация: продажа полного доступа за 100$, партнёрка, доп. продукты\n\n"
        "<b>Домашка:</b> нарисуй схему своей воронки: из какого источника идёт трафик, "
        "какие экраны он видит в боте и где именно ты зарабатываешь.",
    ),
    (
        "5️⃣ Модуль 5. Аналитика и оптимизация связок",
        "📊 <b>Модуль 5. Аналитика</b>\n\n"
        "Без цифр ты не понимаешь, работает ли вообще твоя система.\n\n"
        "Разберём:\n"
        "• какие ключевые метрики отслеживать (CTR, конверсии, стоимость лида/покупки)\n"
        "• как считать, сколько ты зарабатываешь с одного подписчика в боте\n"
        "• как принимать решения: масштабировать связку или искать новую\n"
        "• простые таблицы/дашборды для старта\n\n"
        "<b>Домашка:</b> создай таблицу, где будешь фиксировать: сколько людей пришло, откуда, "
        "сколько оплатило полный доступ и какой доход с них получился.",
    ),
    (
        "6️⃣ Модуль 6. Масштабирование и выстраивание партнёрской сети",
        "🏗 <b>Модуль 6. Масштабирование</b>\n\n"
        "Когда базовая связка работает, задача — аккуратно масштабировать.\n\n"
        "Разберём:\n"
        "• как повышать объёмы трафика, не убивая конверсии\n"
        "• как подключать других людей к переливу (по партнёрке)\n"
        "• как обучать партнёров, чтобы они не сливали трафик впустую\n"
        "• как не перегореть самому и выстроить рабочий ритм\n\n"
        "<b>Домашка:</b> пропиши план масштабирования на 1–3 месяца: какие источники трафика подключаешь, "
        "какие метрики считаешь «нормой» и в каком моменте перерастаешь текущую модель.",
    ),
]

# ---------------------------------------------------------------------------
# КЛАВИАТУРЫ
# ---------------------------------------------------------------------------


def main_reply_kb(is_admin: bool = False):
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(
        KeyboardButton("🧠 Обучение"),
        KeyboardButton("💸 Заработок"),
        KeyboardButton("👤 Профиль"),
    )
    # доп. кнопка только для админа
    if is_admin:
        kb.add(KeyboardButton("🛠 Админ панель"))
    return kb

def admin_inline_kb():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("👥 Пользователи", callback_data="admin_users"))
    kb.add(InlineKeyboardButton("📤 Экспорт пользователей", callback_data="admin_export_users"))
    return kb



def start_inline_kb():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("ℹ️ Как это работает", callback_data="home_how"))
    return kb


def edu_main_kb():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("📈 Курс по трейдингу", callback_data="edu_crypto"))
    kb.add(InlineKeyboardButton("🚀 Курс по трафику", callback_data="edu_traffic"))
    kb.add(InlineKeyboardButton("⬅️ В начало", callback_data="back_home"))
    return kb


def back_to_edu_kb():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("⬅️ Назад к обучению", callback_data="home_edu"))
    return kb


def earn_main_kb(has_access: bool):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("📎 Подробнее про партнёрку", callback_data="earn_more"))
    kb.add(InlineKeyboardButton("📡 Канал с сигналами", callback_data="signals_channel"))
    kb.add(InlineKeyboardButton("👤 Профиль и статистика", callback_data="home_profile"))

    # После покупки полного доступа скрываем кнопку оплаты
    if not has_access:
        kb.add(InlineKeyboardButton("💳 Открыть полный доступ ($100)", callback_data="open_access"))

    kb.add(InlineKeyboardButton("⬅️ В начало", callback_data="back_home"))
    return kb




def profile_kb(has_access: bool, has_signals: bool):
    kb = InlineKeyboardMarkup()

    # Верхний блок — партнёрка и статистика
    kb.add(InlineKeyboardButton("📊 Моя статистика", callback_data="earn_stats"))

    if has_access:
        kb.add(InlineKeyboardButton("🔗 Моя реферальная ссылка", callback_data="my_ref"))
        if not has_signals:
            kb.add(InlineKeyboardButton("📥 Оплатить продление сигналов", callback_data="renew_signals"))
    else:
        kb.add(InlineKeyboardButton("💳 Открыть полный доступ ($100)", callback_data="open_access"))

    # Остальные полезные разделы
    kb.add(InlineKeyboardButton("🏆 Топ партнёров", callback_data="earn_top"))   
    kb.add(InlineKeyboardButton("ℹ️ FAQ", callback_data="faq"))
    kb.add(InlineKeyboardButton("💬 Поддержка", callback_data="support"))
    kb.add(InlineKeyboardButton("⬅️ В начало", callback_data="back_home"))
    return kb



def payment_kb(purchase_id: int, back_cb: str):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("✅ Проверить оплату", callback_data=f"check_pay:{purchase_id}"))
    kb.add(InlineKeyboardButton("🆘 Подтвердить вручную (TXID)", callback_data=f"manual_pay:{purchase_id}"))
    kb.add(InlineKeyboardButton("⬅️ Назад", callback_data=back_cb))
    return kb


def crypto_modules_kb():
    kb = InlineKeyboardMarkup()
    for idx, (title, _) in enumerate(COURSE_CRYPTO):
        kb.add(InlineKeyboardButton(title, callback_data=f"crypto_mod:{idx}"))
    kb.add(InlineKeyboardButton("🔗 Канал с обучением по трейдингу", url=TRADING_EDU_CHANNEL))
    kb.add(InlineKeyboardButton("⬅️ Назад к обучению", callback_data="home_edu"))
    return kb


def traffic_modules_kb():
    kb = InlineKeyboardMarkup()
    for idx, (title, _) in enumerate(COURSE_TRAFFIC):
        kb.add(InlineKeyboardButton(title, callback_data=f"traffic_mod:{idx}"))
    kb.add(InlineKeyboardButton("🔗 Канал с обучением по трафику", url=TRAFFIC_EDU_CHANNEL))
    kb.add(InlineKeyboardButton("⬅️ Назад к обучению", callback_data="home_edu"))
    return kb


# ---------------------------------------------------------------------------
# /START + РЕФЕРАЛКА
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# CHANNEL: если сигнал публикуется вручную в канале (в нашем формате) — сохраняем его для TP/SL
# ---------------------------------------------------------------------------

@dp.channel_post_handler(content_types=types.ContentType.TEXT)
async def channel_capture_signal_posts(message: types.Message):
    if message.chat.id != SIGNALS_CHANNEL_ID:
        return
    try:
        save_signal_trade(message.message_id, message.text or "")
    except Exception:
        pass

@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    if is_spam(message.from_user.id):
        return

    # Парсим реферальный код: /start ref_123456789
    args = message.get_args()
    referrer_db_id = None
    if args and args.startswith("ref_"):
        try:
            ref_tg_id = int(args.split("_", 1)[1])
            if ref_tg_id != message.from_user.id:
                conn = db_connect()
                cur = conn.cursor()
                cur.execute("SELECT id FROM users WHERE user_id = ?", (ref_tg_id,))
                row = cur.fetchone()
                conn.close()
                if row:
                    referrer_db_id = row[0]
        except Exception:
            pass

    user_db_id = get_or_create_user(message, referrer_db_id)

    text = (
    "⚡️ <b>Готовая система под ключ:</b> обучение + сигналы + партнёрка.\n\n"
    "📚 14 модулей (трейдинг + трафик)\n"
    "📡 Закрытый канал с сигналами\n"
    "🤝 Партнёрка <b>50% / 10%</b>\n\n"
    "🎟 <b>Полный доступ — $100</b> (обучение и партнёрка навсегда, сигналы — 1 месяц)\n"
    "Жми «💸 Заработок» — подключу в 2 клика 👇"
    )

    await message.answer(
        text,
        reply_markup=main_reply_kb(is_admin=is_admin(message.from_user.id)),
    )
    await message.answer("Узнать подрорбнее 👇", reply_markup=start_inline_kb())
    
    

def how_back_kb():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="back_home"))
    return kb



# ---------------------------------------------------------------------------
# ОБЩИЕ ХЭНДЛЕРЫ ГЛАВНЫХ КНОПОК
# ---------------------------------------------------------------------------


@dp.message_handler(lambda m: m.text == "🧠 Обучение")
async def msg_edu(message: types.Message):
    if is_spam(message.from_user.id):
        return
    await send_edu_main(message)


@dp.message_handler(lambda m: m.text == "💸 Заработок")
async def msg_earn(message: types.Message):
    if is_spam(message.from_user.id):
        return
    await send_earn_main(message)


@dp.message_handler(lambda m: m.text == "👤 Профиль")
async def msg_profile(message: types.Message):
    if is_spam(message.from_user.id):
        return
    await send_profile(message)
    
@dp.message_handler(lambda m: m.text == "🛠 Админ панель")
async def msg_admin_panel(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    conn = db_connect()
    cur = conn.cursor()

    # всего пользователей
    cur.execute("SELECT COUNT(*) FROM users")
    total_users = cur.fetchone()[0]

    # с полным доступом
    cur.execute("SELECT COUNT(*) FROM users WHERE full_access = 1")
    full_access_users = cur.fetchone()[0]

    # активная подписка на сигналы (по дате)
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        "SELECT COUNT(*) FROM signals_access WHERE active_until IS NOT NULL AND active_until > ?",
        (now,),
    )
    active_signals = cur.fetchone()[0]

    # оплаченные покупки
    cur.execute("SELECT COUNT(*) FROM purchases WHERE status = 'paid'")
    total_paid = cur.fetchone()[0]

    cur.execute(
        "SELECT COUNT(*) FROM purchases WHERE status = 'paid' AND product_code = 'package'"
    )
    paid_packages = cur.fetchone()[0]

    # общий оплаченный объём
    cur.execute("SELECT COALESCE(SUM(amount), 0) FROM purchases WHERE status = 'paid'")
    total_volume = cur.fetchone()[0] or 0

    conn.close()

    text = (
        "🔐 <b>Админ-панель</b>\n\n"
        f"👥 Всего пользователей: <b>{total_users}</b>\n"
        f"✅ С полным доступом: <b>{full_access_users}</b>\n"
        f"📡 Активная подписка на сигналы: <b>{active_signals}</b>\n\n"
        f"💳 Оплаченных покупок всего: <b>{total_paid}</b>\n"
        f"🏷 Пакет за 100$: <b>{paid_packages}</b>\n"
        f"💰 Общий оплаченный объём: <b>{Decimal(str(total_volume)).quantize(Decimal('0.01'))}$</b>\n\n"
        "Выбери действие ниже 👇"
    )

    await message.answer(text, reply_markup=admin_inline_kb())



# ---------------------------------------------------------------------------
# CALLBACK: ГЛАВНОЕ МЕНЮ (ИНЛАЙН)
# ---------------------------------------------------------------------------


@dp.callback_query_handler(lambda c: c.data == "home_edu")
async def cb_home_edu(call: CallbackQuery):
    await send_edu_main(call.message, edit=True)
    await call.answer()


@dp.callback_query_handler(lambda c: c.data == "home_earn")
async def cb_home_earn(call: CallbackQuery):
    fake_msg = call.message
    fake_msg.from_user = call.from_user
    await send_earn_main(fake_msg, edit=True)
    await call.answer()



@dp.callback_query_handler(lambda c: c.data == "home_profile")
async def cb_home_profile(call: CallbackQuery):
    fake_msg = call.message
    fake_msg.from_user = call.from_user  # важно: подставляем реального юзера
    await send_profile(fake_msg, edit=True)
    await call.answer()



@dp.callback_query_handler(lambda c: c.data == "home_how")
async def cb_home_how(call: CallbackQuery):
    text = (
        "ℹ️ <b>Как всё устроено</b>\n\n"
"📦 <b>За $100 ты получаешь:</b>\n"
"• Обучение по трейдингу и трафику (14 модулей)\n"
"• 1 месяц доступа в закрытый канал с сигналами\n"
"• Партнёрскую программу 50% + 10%\n"
"• Личный кабинет с рефералкой и статистикой\n\n"
"Обучение и партнёрка — <b>навсегда</b>, сигналы — по подписке ($50 в месяц).\n\n"
"🤝 <b>Партнёрка:</b> 50% с 1-го уровня и 10% со 2-го.\n\n"
"⚠️ <b>Важно:</b> крипта и трейдинг — это риск, гарантий дохода нет.\n"
"Все решения по сделкам ты принимаешь сам.\n\n"

    )
    try:
        await call.message.edit_text(text, reply_markup=how_back_kb())
    except Exception:
        await call.message.answer(text, reply_markup=how_back_kb())
    await call.answer()

@dp.callback_query_handler(lambda c: c.data == "back_home")
async def cb_back_home(call: CallbackQuery):
    # просто снова покажем стартовое меню
    fake_msg = call.message
    fake_msg.from_user = call.from_user  # чтобы в send_* использовать tg_id
    await cmd_start(fake_msg)
    await call.answer()


# ---------------------------------------------------------------------------
# ОБУЧЕНИЕ
# ---------------------------------------------------------------------------


async def send_edu_main(message: types.Message, edit: bool = False):
    text = (
        "🧠 <b>Обучение внутри экосистемы</b>\n\n"
        "Ты получаешь два направления:\n\n"
        "1️⃣ <b>Крипто-трейдинг</b> — 8 модулей от базовой теории до системного подхода и риск-менеджмента.\n"
        "2️⃣ <b>Перелив трафика и работа с офферами</b> — 6 модулей по источникам трафика, креативам и связкам.\n\n"
        "3️⃣ <b>Работа с сигналами</b> — отдельный блок про то, как правильно пользоваться нашим "
        "сигнальным каналом: какой объём ставить, где ставить стоп, как не сливать депозит на эмоциях.\n\n"
        "Часть материалов — выжимка из платных программ, которые мы покупали у топовых трейдеров и "
        "арбитражников суммарно более чем на <b>$15 000</b>.\n\n"
        "Начни с того, что тебе ближе 👇"
    )
    kb = edu_main_kb()
    if edit:
        try:
            await message.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=kb)


@dp.callback_query_handler(lambda c: c.data == "edu_structure")
async def cb_edu_structure(call: CallbackQuery):
    # Структура трейдинга
    lines = ["📚 <b>Структура курса по трейдингу (8 модулей)</b>\n"]
    for title, _ in COURSE_CRYPTO:
        lines.append(f"• {title}")
    lines.append("\nНажми кнопку ниже, чтобы перейти к курсу.")
    text_crypto = "\n".join(lines)

    # Структура трафика 
    lines2 = ["📚 <b>Структура курса по трафику (6 модулей)</b>\n"]
    for title, _ in COURSE_TRAFFIC:
        lines2.append(f"• {title}")
    lines2.append("\nНажми кнопку ниже, чтобы перейти к курсу.")
    text_traffic = "\n".join(lines2)

    kb_crypto = InlineKeyboardMarkup()
    kb_crypto.add(InlineKeyboardButton("📈 Перейти к курсу по трейдингу", callback_data="edu_crypto"))
    kb_crypto.add(InlineKeyboardButton("⬅️ Назад к обучению", callback_data="home_edu"))

    kb_traffic = InlineKeyboardMarkup()
    kb_traffic.add(InlineKeyboardButton("🚀 Перейти к курсу по трафику", callback_data="edu_traffic"))
    kb_traffic.add(InlineKeyboardButton("⬅️ Назад к обучению", callback_data="home_edu"))

    await call.message.answer(text_crypto, reply_markup=kb_crypto)
    await call.message.answer(text_traffic, reply_markup=kb_traffic)
    await call.answer()


def _get_user_db_id(tg_id: int) -> int | None:
    row = get_user_by_tg(tg_id)
    return row[0] if row else None


@dp.callback_query_handler(lambda c: c.data == "edu_crypto")
async def cb_edu_crypto(call: CallbackQuery):
    user_row = get_user_by_tg(call.from_user.id)
    if not user_row:
        get_or_create_user(call.message)
        user_row = get_user_by_tg(call.from_user.id)
    user_db_id = user_row[0]
    full = bool(user_row[7])

    if not full:
        text = (
            "📈 <b>Курс по трейдингу</b>\n\n"
            "Курс доступен после покупки полного доступа за <b>$100</b>.\n\n"
            "Ты получаешь 8 модулей с системным подходом к крипто-торговле, плюс доступ к трафику, "
            "сигналам и партнёрке.\n\n"
            "Чтобы открыть курс — оформи полный доступ."
        )
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("📚 Посмотреть структуру", callback_data="edu_structure"))
        kb.add(InlineKeyboardButton("💳 Открыть полный доступ", callback_data="open_access"))
        kb.add(InlineKeyboardButton("⬅️ Назад к обучению", callback_data="home_edu"))
        try:
            await call.message.edit_text(text, reply_markup=kb)
        except Exception:
            await call.message.answer(text, reply_markup=kb)
    else:
        text = "📈 <b>Курс по трейдингу</b>\n\n✅ У тебя открыт полный доступ. Выбери модуль 👇"
        try:
            await call.message.edit_text(text, reply_markup=crypto_modules_kb())
        except Exception:
            await call.message.answer(text, reply_markup=crypto_modules_kb())
    await call.answer()


@dp.callback_query_handler(lambda c: c.data == "edu_traffic")
async def cb_edu_traffic(call: CallbackQuery):
    user_row = get_user_by_tg(call.from_user.id)
    if not user_row:
        get_or_create_user(call.message)
        user_row = get_user_by_tg(call.from_user.id)
    user_db_id = user_row[0]
    full = bool(user_row[7])

    if not full:
        text = (
            "🚀 <b>Курс по переливу трафика</b>\n\n"
            "Доступ к курсу открывается после покупки полного доступа за <b>$100</b>.\n\n"
            "Внутри 6 модулей по источникам трафика, связкам, креативам и аналитике.\n\n"
            "Чтобы открыть курс — оформи полный доступ."
        )
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("📚 Посмотреть структуру", callback_data="edu_structure"))
        kb.add(InlineKeyboardButton("💳 Открыть полный доступ", callback_data="open_access"))
        kb.add(InlineKeyboardButton("⬅️ Назад к обучению", callback_data="home_edu"))
        try:
            await call.message.edit_text(text, reply_markup=kb)
        except Exception:
            await call.message.answer(text, reply_markup=kb)
    else:
        text = "🚀 <b>Курс по трафику</b>\n\n✅ Курс доступен. Выбери модуль 👇"
        try:
            await call.message.edit_text(text, reply_markup=traffic_modules_kb())
        except Exception:
            await call.message.answer(text, reply_markup=traffic_modules_kb())
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("crypto_mod:"))
async def cb_crypto_mod(call: CallbackQuery):
    idx = int(call.data.split(":")[1])
    if idx < 0 or idx >= len(COURSE_CRYPTO):
        await call.answer("Модуль не найден", show_alert=True)
        return

    user_row = get_user_by_tg(call.from_user.id)
    if not user_row or not user_row[7]:
        await call.answer("Курс доступен только после покупки полного доступа.", show_alert=True)
        return

    user_db_id = user_row[0]
    save_progress(user_db_id, "crypto", idx)

    title, text_body = COURSE_CRYPTO[idx]
    text = f"{text_body}\n\nПрогресс: модуль {idx+1} из {len(COURSE_CRYPTO)}."
    kb = crypto_modules_kb()
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("traffic_mod:"))
async def cb_traffic_mod(call: CallbackQuery):
    idx = int(call.data.split(":")[1])
    if idx < 0 or idx >= len(COURSE_TRAFFIC):
        await call.answer("Модуль не найден", show_alert=True)
        return

    user_row = get_user_by_tg(call.from_user.id)
    if not user_row or not user_row[7]:
        await call.answer("Курс доступен только после покупки полного доступа.", show_alert=True)
        return

    user_db_id = user_row[0]
    save_progress(user_db_id, "traffic", idx)

    title, text_body = COURSE_TRAFFIC[idx]
    text = f"{text_body}\n\nПрогресс: модуль {idx+1} из {len(COURSE_TRAFFIC)}."
    kb = traffic_modules_kb()
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)
    await call.answer()


# ---------------------------------------------------------------------------
# ЗАРАБОТОК / ПАРТНЁРКА 
# ---------------------------------------------------------------------------


async def send_earn_main(message: types.Message, edit: bool = False):
    # Проверяем доступ пользователя
    user_row = get_user_by_tg(message.from_user.id)
    if not user_row:
        get_or_create_user(message)
        user_row = get_user_by_tg(message.from_user.id)

    has_access = bool(user_row and user_row[7])

    text = (
        "💸 <b>Заработок</b>\n\n"
        "🤝 Здесь ты можешь зарабатывать на партнёрской программе.\n\n"
        "💰 <b>Вознаграждение:</b>\n"
        "• <b>50%</b> — с 1-го уровня\n"
        "• <b>10%</b> — со 2-го уровня\n\n"
        "📌 <b>Как это работает:</b>\n"
        "1️⃣ Открываешь полный доступ за <b>$100</b>\n"
        "2️⃣ Забираешь реферальную ссылку в профиле\n"
        "3️⃣ Приглашаешь людей и получаешь начисления\n\n"
        "📊 Вся статистика — в профиле 👤"
    )

    if has_access:
        text += "\n\n✅ <b>Полный доступ уже активен</b> — можешь сразу приглашать людей."
    else:
        text += "\n\n🔓 <b>Полный доступ ещё не активирован</b> — оформи доступ, чтобы включить партнёрку."

    kb = earn_main_kb(has_access)

    if edit:
        try:
            await message.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass

    await message.answer(text, reply_markup=kb)



@dp.callback_query_handler(lambda c: c.data == "earn_more")
async def cb_earn_more(call: CallbackQuery):
    user_row = get_user_by_tg(call.from_user.id)
    if not user_row:
        await call.answer("Сначала запусти бота через /start.", show_alert=True)
        return

    has_access = bool(user_row[7])

    text = (
        "🤝 <b>Партнёрская программа 50% + 10%</b>\n\n"
        "Ты зарабатываешь, когда твои приглашённые покупают <b>полный доступ</b>.\n\n"
        "💰 <b>Начисления:</b>\n"
        "• 50% — 1-й уровень\n"
        "• 10% — 2-й уровень\n\n"
        "🔗 Реферальная ссылка и вся статистика — в профиле 👤\n"
        "⚠️ Начисление идёт за <b>первую покупку полного доступа</b> человеком."
    )

    if has_access:
        text += "\n\n✅ <b>Полный доступ уже активен</b> — забирай ссылку в профиле и приглашай людей."
    else:
        text += "\n\n🔓 Чтобы открыть реферальную ссылку — оформи полный доступ."

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("📊 Моя статистика", callback_data="earn_stats"))
    kb.add(InlineKeyboardButton("👤 Профиль и статистика", callback_data="home_profile"))

    if not has_access:
        kb.add(InlineKeyboardButton("💳 Открыть полный доступ", callback_data="open_access"))

    kb.add(InlineKeyboardButton("⬅️ Назад к разделу «Заработок»", callback_data="home_earn"))

    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)

    await call.answer()



@dp.callback_query_handler(lambda c: c.data == "earn_stats")
async def       ts(call: CallbackQuery):
    user_row = get_user_by_tg(call.from_user.id)
    if not user_row:
        await call.answer("Сначала запусти бота через /start.", show_alert=True)
        return

    user_db_id, _, username, first_name, _, balance, total_earned, full_access = user_row
    lvl1, lvl2 = count_referrals(user_db_id)
    total_refs = lvl1 + lvl2

    balance_dec = Decimal(str(balance)).quantize(Decimal("0.01"))
    total_earned_dec = Decimal(str(total_earned)).quantize(Decimal("0.01"))

    pending_withdraw = get_pending_withdraw(user_db_id)
    if pending_withdraw:
        withdraw_status = "есть активная заявка на проверке ⏳"
    elif balance_dec > Decimal("0"):
        withdraw_status = "средства доступны для вывода ✅"
    else:
        withdraw_status = "пока выводить нечего ❌"

    text = (
        "📊 <b>Твоя партнёрская статистика</b>\n\n"
        f"Имя: <b>{first_name}</b>\n"
        f"Логин: @{username if username else '—'}\n\n"
        f"Партнёров 1 уровня: <b>{lvl1}</b>\n"
        f"Партнёров 2 уровня: <b>{lvl2}</b>\n"
        f"Всего приглашено: <b>{total_refs}</b>\n\n"
        f"Баланс к выводу: <b>{balance_dec}$</b>\n"
        f"Всего заработано: <b>{total_earned_dec}$</b>\n\n"
        f"Статус доступа: <b>{'Полный доступ есть ✅' if full_access else 'Полный доступ не оплачен ❌'}</b>\n"
        f"Статус вывода: <b>{withdraw_status}</b>\n\n"
        "Как только на балансе есть деньги, ты можешь оформить заявку на вывод прямо из бота 💵"
    )

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🔗 Моя реферальная ссылка", callback_data="my_ref"))
    kb.add(InlineKeyboardButton("🏆 Топ партнёров", callback_data="earn_top"))

    # Кнопка заявки на вывод:
    # – есть полный доступ
    # – есть баланс > 0
    # – нет активной заявки
    if full_access and balance_dec > Decimal("0") and not pending_withdraw:
        kb.add(InlineKeyboardButton("💵 Заявка на вывод", callback_data="withdraw_request"))

    kb.add(InlineKeyboardButton("⬅️ Назад к разделу «Заработок»", callback_data="home_earn"))

    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)
    await call.answer()



@dp.callback_query_handler(lambda c: c.data == "earn_top")
async def cb_earn_top(call: CallbackQuery):
    # Топ по количеству рефералов 1 уровня
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT u.username, u.first_name, COUNT(r.id) as cnt
        FROM users u
        LEFT JOIN users r ON r.referrer_id = u.id
        GROUP BY u.id
        HAVING cnt > 0
        ORDER BY cnt DESC
        LIMIT 10
        """
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        text = "🏆 Пока ещё нет партнёров в топе. Стань первым!"
    else:
        lines = ["🏆 <b>Топ партнёров по количеству приглашённых</b>\n"]
        for i, (username, first_name, cnt) in enumerate(rows, start=1):
            name = f"@{username}" if username else first_name or "Без имени"
            lines.append(f"{i}. {name} — {cnt} приглашённых")
        text = "\n".join(lines)

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("📊 Моя статистика", callback_data="earn_stats"))
    kb.add(InlineKeyboardButton("⬅️ Назад к разделу «Заработок»", callback_data="home_earn"))

    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)
    await call.answer()


@dp.callback_query_handler(lambda c: c.data == "withdraw_request")
async def cb_withdraw_request(call: CallbackQuery):
    user_row = get_user_by_tg(call.from_user.id)
    if not user_row:
        await call.answer("Сначала запусти бота через /start.", show_alert=True)
        return

    user_db_id, tg_id, username, first_name, _, balance, total_earned, full_access = user_row

    if not full_access:
        await call.answer("Заявка на вывод доступна только после покупки полного доступа.", show_alert=True)
        return

    balance_dec = Decimal(str(balance)).quantize(Decimal("0.01"))
    if balance_dec <= Decimal("0"):
        await call.answer("Сначала накопи баланс к выводу.", show_alert=True)
        return

    pending = get_pending_withdraw(user_db_id)
    if pending:
        await call.answer("У тебя уже есть активная заявка. Дождись её обработки 🙌", show_alert=True)
        return

    # 1) создаём заявку в таблице withdrawals
    create_withdraw_request(user_db_id, balance_dec)

    # 2) обнуляем баланс пользователя
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("UPDATE users SET balance = 0 WHERE id = ?", (user_db_id,))
    conn.commit()
    conn.close()

    text = (
        "💵 <b>Заявка на вывод отправлена</b>\n\n"
        f"Сумма к выплате: <b>{balance_dec}$</b>\n\n"
        "Мы получили твою заявку и передали её администратору.\n"
        "Выплаты делаются вручную, в рабочее время.\n\n"
        "Если прошло много времени и деньги не пришли — просто напиши в поддержку с пометкой "
        "«вывод партнёрки»."
    )

    try:
        await call.message.edit_text(text)
    except Exception:
        await call.message.answer(text)

    # Уведомление админу
    try:
        name = f"@{username}" if username else (first_name or str(tg_id))
        await bot.send_message(
            ADMIN_ID,
            "📥 <b>Новая заявка на вывод партнёрки</b>\n\n"
            f"Пользователь: {name}\n"
            f"TG ID: <code>{tg_id}</code>\n"
            f"ID в БД: <code>{user_db_id}</code>\n"
            f"Сумма: <b>{balance_dec}$</b>\n\n"
            "После выплаты не забудь отметить заявку как обработанную "
            "в таблице <code>withdrawals</code>.",
        )
    except Exception:
        pass

    await call.answer("Заявка отправлена ✅", show_alert=True)



@dp.callback_query_handler(lambda c: c.data == "my_ref")
async def cb_my_ref(call: CallbackQuery):
    user_row = get_user_by_tg(call.from_user.id)
    if not user_row:
        await call.answer("Сначала запусти бота через /start.", show_alert=True)
        return

    user_db_id, user_tg_id, username, first_name, _, _, _, full_access = user_row

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("⬅️ Назад в профиль", callback_data="home_profile"))

    # 1) Если доступа нет — показываем “купи доступ”
    if not full_access:
        text = (
            "🔗 <b>Реферальная ссылка</b>\n\n"
            "Чтобы получить реферальную ссылку, нужно открыть полный доступ за <b>$100</b>.\n\n"
            "После покупки ссылка появится здесь ✅"
        )
    else:
        # 2) Если доступ есть — показываем ссылку
        me = await bot.get_me()
        ref_link = f"https://t.me/{me.username}?start=ref_{user_tg_id}"

        text = (
            "🔗 <b>Твоя реферальная ссылка</b>\n\n"
            f"<code>{ref_link}</code>\n\n"
            "Делись ей с людьми, которые хотят:\n"
            "• разобраться в трейдинге 📈\n"
            "• научиться переливать трафик 🚀\n"
            "• зарабатывать по партнёрской программе 🤝"
        )

    try:
        await call.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await call.message.answer(text, reply_markup=kb, disable_web_page_preview=True)

    await call.answer()

 
@dp.callback_query_handler(lambda c: c.data == "signals_channel")
async def cb_signals_channel(call: CallbackQuery):
    user_row = get_user_by_tg(call.from_user.id)
    if not user_row:
        await call.answer("Сначала запусти бота через /start.", show_alert=True)
        return

    user_db_id, user_tg_id, username, first_name, _, _, _, full_access = user_row
    signals_until = get_signals_until(user_db_id)

    # 1) Полного доступа ещё нет → предлагаем купить пакет за $100
    if not full_access:
        text = (
            "📡 <b>Канал с сигналами</b>\n\n"
            "Доступ к сигналам открывается после покупки полного доступа за <b>$100</b>.\n\n"
            "Ты получаешь:\n"
            "• обучение по трейдингу (8 модулей)\n"
            "• обучение по трафику (6 модулей)\n"
            "• 1 месяц доступа к сигналам\n"
            "• партнёрскую программу 50% + 10%\n\n"
            "Чтобы попасть в канал — оформи полный доступ."
        )
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("💳 Открыть полный доступ ($100)", callback_data="open_access"))
        kb.add(InlineKeyboardButton("⬅️ Назад к разделу «Заработок»", callback_data="home_earn"))
        try:
            await call.message.edit_text(text, reply_markup=kb)
        except Exception:
            await call.message.answer(text, reply_markup=kb)
        await call.answer()
        return

    # 2) Полный доступ есть, но подписка на сигналы не активна → просим оплатить продление
    now = datetime.utcnow()
    if not signals_until or signals_until < now:
        text = (
            "📡 <b>Канал с сигналами</b>\n\n"
            "Сейчас твоя подписка на сигналы <b>не активна</b>.\n\n"
            "Чтобы снова получать сигналы, оплати продление за <b>$50</b> на 1 месяц."
        )
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("💳 Оплатить продление сигналов ($50)", callback_data="renew_signals"))
        kb.add(InlineKeyboardButton("⬅️ Назад к разделу «Заработок»", callback_data="home_earn"))
        try:
            await call.message.edit_text(text, reply_markup=kb)
        except Exception:
            await call.message.answer(text, reply_markup=kb)
        await call.answer()
        return

    # 3) Всё оплачено и подписка активна → даём ссылку на канал
    text = (
        "📡 <b>Канал с сигналами</b>\n\n"
        f"Твоя подписка активна до: <b>{signals_until.strftime('%Y-%m-%d')}</b>.\n\n"
        "Нажми кнопку ниже, чтобы перейти в закрытый канал."
    )
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("📡 Открыть канал с сигналами", url=SIGNALS_CHANNEL_LINK))
    kb.add(InlineKeyboardButton("⬅️ Назад к разделу «Заработок»", callback_data="home_earn"))
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)
    await call.answer()



# ---------------------------------------------------------------------------
# ПРОФИЛЬ / ОПЛАТА
# ---------------------------------------------------------------------------


async def send_profile(message: types.Message, edit: bool = False):
    user_row = get_user_by_tg(message.from_user.id)
    if not user_row:
        get_or_create_user(message)
        user_row = get_user_by_tg(message.from_user.id)

    user_db_id, user_tg_id, username, first_name, _, balance, total_earned, full_access = user_row
    lvl1, lvl2 = count_referrals(user_db_id)
    signals_until = get_signals_until(user_db_id)

    # Прогресс обучения
    crypto_idx = get_progress(user_db_id, "crypto")
    traffic_idx = get_progress(user_db_id, "traffic")
    crypto_done = max(0, crypto_idx + 1) if crypto_idx >= 0 else 0
    traffic_done = max(0, traffic_idx + 1) if traffic_idx >= 0 else 0

    text_lines = [
        "👤 <b>Твой профиль</b>\n",
        f"• Ник: @{username if username else '—'}",
        f"• ID: <code>{user_tg_id}</code>\n",
        f"• Полный доступ: {'есть ✅' if full_access else 'нет ❌'}",
    ]

    now = datetime.utcnow()

    if signals_until and signals_until > now:
        text_lines.append(f"• Подписка на сигналы активна до: <b>{signals_until.strftime('%Y-%m-%d')}</b> ✅")
        has_signals = True
    elif signals_until:
        text_lines.append(f"• Подписка на сигналы: <b>истекла</b> ({signals_until.strftime('%Y-%m-%d')}) ❌")
        has_signals = False
    else:
        text_lines.append("• Подписка на сигналы: <b>не активна</b> ❌")
        has_signals = False


    text_lines.extend(
        [
            "",
            f"• Рефералов 1 уровня: <b>{lvl1}</b>",
            f"• Рефералов 2 уровня: <b>{lvl2}</b>",
            f"• Баланс к выводу: <b>{Decimal(str(balance)).quantize(Decimal('0.01'))}$</b>",
            f"• Всего заработано: <b>{Decimal(str(total_earned)).quantize(Decimal('0.01'))}$</b>",
            "",
            f"• Прогресс трейдинга: <b>{crypto_done}/{len(COURSE_CRYPTO)} модулей</b>",
            f"• Прогресс трафика: <b>{traffic_done}/{len(COURSE_TRAFFIC)} модулей</b>",
        ]
    )

    text = "\n".join(text_lines)
    kb = profile_kb(bool(full_access), has_signals)

    if edit:
        try:
            await message.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=kb)


@dp.callback_query_handler(lambda c: c.data == "faq")
async def cb_faq(call: CallbackQuery):
    text = (
        "ℹ️ <b>FAQ</b>\n\n"
        "❓ <b>Что входит в полный доступ за $100?</b>\n"
        "• Обучение по трейдингу (8 модулей)\n"
        "• Обучение по трафику (6 модулей)\n"
        "• Доступ к сигналам на 1 месяц\n"
        "• Партнёрская программа 50% + 10%\n"
        "• Личный кабинет и статистика\n\n"
        "❓ <b>Можно ли вернуть деньги после оплаты?</b>\n"
        "Нет. Сразу после оплаты открывается доступ ко всем закрытым материалам и партнёрке, "
        "поэтому возврат средств не предусмотрен.\n\n"
        "❓ <b>С чего идёт партнёрское вознаграждение?</b>\n"
        "Вознаграждение начисляется с покупки полного доступа за $100.\n\n"
        "❓ <b>Что делать, если оплата прошла, а доступ не открылся?</b>\n"
        "Напиши в поддержку, укажи сумму, время и хэш транзакции — мы проверим вручную.\n\n"
        "❓ <b>Какие риски связаны с криптой и сигналами?</b>\n"
        "Криптовалюта и трейдинг всегда связаны с риском. Нет гарантированного дохода. "
        "Сигналы и обучение — это инструменты, а решения принимаешь ты."
    )
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("💬 Написать в поддержку", callback_data="support"))
    kb.add(InlineKeyboardButton("⬅️ Назад в профиль", callback_data="home_profile"))

    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)
    await call.answer()


@dp.callback_query_handler(lambda c: c.data == "support")
async def cb_support(call: CallbackQuery):
    text = (
        "💬 <b>Поддержка</b>\n\n"
        f"Если возникли вопросы — напиши в поддержку: {SUPPORT_CONTACT}\n\n"
        "Опиши ситуацию одним сообщением, приложи скрины / хэш транзакции при необходимости."
    )
    try:
        await call.message.edit_text(text)
    except Exception:
        await call.message.answer(text)
    await call.answer()


# ---------------------- ОПЛАТА ПОЛНОГО ДОСТУПА -----------------------


@dp.callback_query_handler(lambda c: c.data == "open_access")
async def cb_open_access(call: CallbackQuery):
    user_row = get_user_by_tg(call.from_user.id)
    if not user_row:
        get_or_create_user(call.message)
        user_row = get_user_by_tg(call.from_user.id)
    user_db_id = user_row[0]

    purchase_id = create_purchase(user_db_id, "package", PRICE_PACKAGE)
    purchase_row = get_purchase(purchase_id)
    amount = Decimal(str(purchase_row[3]))

    text = (
        "💳 <b>Открытие полного доступа за $100</b>\n\n"
        "Ты получаешь:\n"
        "• обучение по трейдингу (8 модулей)\n"
        "• обучение по трафику (6 модулей)\n"
        "• доступ к сигналам на 1 месяц\n"
        "• доступ к партнёрской программе 50% + 10%\n\n"
        f"Оплата принимается в USDT (TRC20) на кошелёк:\n"
        f"<code>{WALLET_ADDRESS}</code>\n\n"
        f"Сумма к оплате: <b>{amount} USDT</b>\n"
        "Важно: переводи <b>точно эту сумму</b> с учётом хвостика — по ней бот будет искать платёж.\n\n"
        "После перевода нажми кнопку «Проверить оплату» ниже.\n"
        "Если оплата не подтянулась — не переживай, транзакции иногда доходят с задержкой, "
        "а также всегда есть ручная проверка через поддержку."
    )

    kb = payment_kb(purchase_id, back_cb="home_profile")

    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)
    await call.answer()


@dp.callback_query_handler(lambda c: c.data == "renew_signals")
async def cb_renew_signals(call: CallbackQuery):
    user_row = get_user_by_tg(call.from_user.id)
    if not user_row:
        await call.answer("Сначала запусти бота через /start.", show_alert=True)
        return
    user_db_id = user_row[0]
    full = bool(user_row[7])

    if not full:
        await call.answer("Продление сигналов доступно только после покупки полного доступа.", show_alert=True)
        return

    purchase_id = create_purchase(user_db_id, "renewal", PRICE_RENEWAL)
    purchase_row = get_purchase(purchase_id)
    amount = Decimal(str(purchase_row[3]))

    text = (
        "📈 <b>Продление сигналов на 1 месяц</b>\n\n"
        "Стоимость продления: <b>$50</b>.\n\n"
        f"Отправь <b>{amount} USDT</b> (TRC20) на кошелёк:\n"
        f"<code>{WALLET_ADDRESS}</code>\n\n"
        "После перевода нажми «Проверить оплату». Реферальные начисления с продлений не идут — "
        "весь платёж идёт на поддержку проекта и развитие экосистемы."
    )

    kb = payment_kb(purchase_id, back_cb="home_profile")

    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("check_pay:"))
async def cb_check_pay(call: CallbackQuery):
    _, pid_str = call.data.split(":", 1)
    try:
        purchase_id = int(pid_str)
    except ValueError:
        await call.answer("Некорректный ID покупки.", show_alert=True)
        return

    # Кулдаун: чтобы не спамили проверкой оплаты и не ловили лимиты
    rem = _cooldown_remaining(call.from_user.id, "check_pay", 30)
    if rem > 0:
        await call.answer(f"⏳ Подожди {rem} сек и попробуй ещё раз.", show_alert=False)
        return

    purchase_row = get_purchase(purchase_id)
    if not purchase_row:
        await call.answer("Покупка не найдена. Напиши в поддержку.", show_alert=True)
        return

    p_id, user_db_id, product_code, amount_f, status, created_at_str, tx_id = purchase_row
    amount = Decimal(str(amount_f))
    created_at = datetime.strptime(created_at_str, "%Y-%m-%d %H:%M:%S")

    if status == "paid":
        await call.answer("Эта покупка уже подтверждена ✅", show_alert=True)
        return

    if not TRONGRID_API_KEY:
        await call.message.answer(
            "⚠️ <b>Автопроверка временно недоступна</b> (нет TronGrid API key).\n\n"
            "Нажми кнопку <b>🆘 Подтвердить вручную (TXID)</b> и отправь хэш транзакции — я передам заявку админу.",
            reply_markup=payment_kb(purchase_id, back_cb="home_profile"),
        )
        await call.answer()
        return

    await call.answer("Ищу оплату в сети Tron, это может занять несколько секунд...")

    tx_hash = await find_payment_for_purchase(amount, created_at)
    if not tx_hash:
        await call.message.answer(
            "❌ Пока не вижу подходящий платёж.\n\n"
            "Проверь: сеть <b>USDT TRC20</b>, сумма <b>точно как указано</b>, адрес правильный.\n"
            "Иногда транзакции появляются с задержкой.\n\n"
            "Если прошло несколько минут и не подтянулось — нажми <b>🆘 Подтвердить вручную (TXID)</b> и пришли хэш транзакции.",
            reply_markup=payment_kb(purchase_id, back_cb="home_profile"),
        )
        return

    # фиксируем оплату
    mark_purchase_paid(purchase_id, tx_hash)
    await process_successful_payment(get_purchase(purchase_id))


# ---------------------------------------------------------------------------
# РУЧНОЕ ПОДТВЕРЖДЕНИЕ ОПЛАТЫ (FALLBACK)
# ---------------------------------------------------------------------------

@dp.callback_query_handler(lambda c: c.data.startswith("manual_pay:"))
async def cb_manual_pay(call: CallbackQuery):
    _, pid_str = call.data.split(":", 1)
    try:
        purchase_id = int(pid_str)
    except ValueError:
        await call.answer("Некорректный ID покупки.", show_alert=True)
        return

    purchase_row = get_purchase(purchase_id)
    if not purchase_row:
        await call.answer("Покупка не найдена.", show_alert=True)
        return

    p_id, user_db_id, product_code, amount_f, status, created_at_str, tx_id = purchase_row

    user_row = get_user_by_tg(call.from_user.id)
    if not user_row or user_row[0] != user_db_id:
        await call.answer("Это не твоя покупка.", show_alert=True)
        return

    if status == "paid":
        await call.answer("Эта покупка уже подтверждена ✅", show_alert=True)
        return

    MANUAL_TX_WAIT[call.from_user.id] = purchase_id
    await call.message.answer(
        "🆘 <b>Ручное подтверждение оплаты</b>\n\n"
        "Отправь одним сообщением <b>TXID</b> (хэш транзакции) — <b>64</b> символа (0-9, a-f).\n"
        "Если передумал — напиши <code>отмена</code>.",
        disable_web_page_preview=True,
    )
    await call.answer()


@dp.message_handler(lambda m: m.from_user and m.from_user.id in MANUAL_TX_WAIT)
async def msg_manual_txid(message: types.Message):
    purchase_id = MANUAL_TX_WAIT.get(message.from_user.id)

    text = (message.text or "").strip()
    if text.lower() in {"отмена", "cancel"}:
        MANUAL_TX_WAIT.pop(message.from_user.id, None)
        await message.answer("Ок, отменил ✅")
        return

    if not re.fullmatch(r"[0-9a-fA-F]{64}", text):
        await message.answer("❌ Это не похоже на TXID. Вставь ровно <b>64</b> символа (0-9, a-f).")
        return

    txid = text

    purchase_row = get_purchase(int(purchase_id))
    if not purchase_row:
        MANUAL_TX_WAIT.pop(message.from_user.id, None)
        await message.answer("Покупка не найдена. Напиши в поддержку.")
        return

    p_id, user_db_id, product_code, amount_f, status, created_at_str, old_tx = purchase_row

    user_row = get_user_by_tg(message.from_user.id)
    if not user_row or user_row[0] != user_db_id:
        MANUAL_TX_WAIT.pop(message.from_user.id, None)
        await message.answer("❌ Эта покупка не принадлежит твоему аккаунту.")
        return

    if status == "paid":
        MANUAL_TX_WAIT.pop(message.from_user.id, None)
        await message.answer("✅ Эта покупка уже подтверждена.")
        return

    if is_txid_used(txid):
        await message.answer("⚠️ Этот TXID уже использован. Проверь, что отправляешь правильный хэш транзакции.")
        return

    req_id = upsert_manual_pay_request(int(purchase_id), message.from_user.id, txid)

    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("✅ Подтвердить", callback_data=f"admin_mpay_ok:{req_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"admin_mpay_no:{req_id}"),
    )

    amount = Decimal(str(amount_f))
    tronscan_link = f"https://tronscan.org/#/transaction/{txid}"

    try:
        await bot.send_message(
            ADMIN_ID,
            "🆘 <b>Запрос на ручное подтверждение оплаты</b>\n\n"
            f"Заявка: <code>{req_id}</code>\n"
            f"Покупка: <code>{purchase_id}</code>\n"
            f"Юзер: <code>{message.from_user.id}</code>\n"
            f"Товар: <b>{product_code}</b>\n"
            f"Сумма: <b>{amount}</b> USDT\n"
            f"TXID: <code>{txid}</code>\n"
            f"TronScan: {tronscan_link}",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception:
        pass

    MANUAL_TX_WAIT.pop(message.from_user.id, None)
    await message.answer("✅ Заявку отправил админу. Как подтвердим — доступ откроется.")


@dp.callback_query_handler(lambda c: c.data.startswith("admin_mpay_ok:"))
async def cb_admin_manual_ok(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer()
        return

    _, rid_str = call.data.split(":", 1)
    try:
        req_id = int(rid_str)
    except ValueError:
        await call.answer("Некорректная заявка.", show_alert=True)
        return

    req = get_manual_pay_request(req_id)
    if not req:
        await call.answer("Заявка не найдена.", show_alert=True)
        return

    _id, purchase_id, tg_user_id, txid, status, created_at, processed_at = req

    if status != "pending":
        await call.answer("Уже обработано.", show_alert=True)
        return

    if is_txid_used(txid):
        await call.answer("TXID уже использован ⚠️", show_alert=True)
        set_manual_pay_request_status(req_id, "rejected")
        try:
            await call.message.edit_text("❌ Отклонено (TXID уже был использован).")
        except Exception:
            pass
        return

    purchase_row = get_purchase(int(purchase_id))
    if not purchase_row:
        await call.answer("Покупка не найдена.", show_alert=True)
        set_manual_pay_request_status(req_id, "rejected")
        try:
            await call.message.edit_text("❌ Отклонено (покупка не найдена).")
        except Exception:
            pass
        return

    p_id, user_db_id, product_code, amount_f, p_status, created_at_str, old_tx = purchase_row
    if p_status == "paid":
        set_manual_pay_request_status(req_id, "approved")
        await call.answer("Покупка уже подтверждена ✅", show_alert=True)
        try:
            await call.message.edit_text("✅ Подтверждено (покупка уже была оплачена).")
        except Exception:
            pass
        return

    mark_purchase_paid(int(purchase_id), str(txid))
    set_manual_pay_request_status(req_id, "approved")
    await process_successful_payment(get_purchase(int(purchase_id)))

    try:
        await call.message.edit_text("✅ Подтверждено и обработано.")
    except Exception:
        pass

    await call.answer("Готово ✅", show_alert=False)


@dp.callback_query_handler(lambda c: c.data.startswith("admin_mpay_no:"))
async def cb_admin_manual_no(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer()
        return

    _, rid_str = call.data.split(":", 1)
    try:
        req_id = int(rid_str)
    except ValueError:
        await call.answer("Некорректная заявка.", show_alert=True)
        return

    req = get_manual_pay_request(req_id)
    if not req:
        await call.answer("Заявка не найдена.", show_alert=True)
        return

    _id, purchase_id, tg_user_id, txid, status, created_at, processed_at = req

    if status != "pending":
        await call.answer("Уже обработано.", show_alert=True)
        return

    set_manual_pay_request_status(req_id, "rejected")

    try:
        await bot.send_message(
            int(tg_user_id),
            "❌ Оплата не подтверждена админом.\n\n"
            "Проверь, что TXID верный и транзакция действительно USDT (TRC20). "
            "Если нужна помощь — напиши в поддержку.",
            disable_web_page_preview=True,
        )
    except Exception:
        pass

    try:
        await call.message.edit_text("❌ Отклонено.")
    except Exception:
        pass

    await call.answer("Отклонено", show_alert=False)

# ---------------------------------------------------------------------------
# АДМИН-КОМАНДЫ (МИНИМАЛЬНЫЙ НАБОР)
# ---------------------------------------------------------------------------


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


@dp.message_handler(commands=["admin"])
async def cmd_admin(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    text = (
        "🔐 <b>Админ-панель</b>\n\n"
        "Доступные команды:\n"
        "/grant &lt;id или @username&gt; — выдать полный доступ + сигналы на 1 месяц\n"
        "/extend_signals &lt;id или @username&gt; — продлить сигналы на 1 месяц\n"
        "/user &lt;id или @username&gt; — инфо по пользователю"
    )
    await message.answer(text)
    
@dp.callback_query_handler(lambda c: c.data == "admin_users")
async def cb_admin_users(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer()
        return

    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, user_id, username, first_name, full_access, reg_date
        FROM users
        ORDER BY id DESC
        LIMIT 50
        """
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        text = "👥 Пользователей пока нет."
    else:
        lines = ["👥 <b>Последние пользователи (до 50 шт.)</b>\n"]
        for uid, tg_id, username, first_name, full_access, reg_date in rows:
            name = f"@{username}" if username else (first_name or "—")
            access = "✅" if full_access else "❌"
            lines.append(
                f"{uid}. {name} | TG: <code>{tg_id}</code> | full_access: {access} | {reg_date}"
            )
        text = "\n".join(lines)

    try:
        await call.message.edit_text(text, reply_markup=admin_inline_kb())
    except Exception:
        await call.message.answer(text, reply_markup=admin_inline_kb())

    await call.answer()


@dp.callback_query_handler(lambda c: c.data == "admin_export_users")
async def cb_admin_export_users(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer()
        return

    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, user_id, username, first_name, referrer_id, balance, total_earned,
               reg_date, full_access, is_blocked
        FROM users
        ORDER BY id ASC
        """
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await call.answer("Пользователей пока нет", show_alert=True)
        return

    # собираем CSV в памяти
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "id",
            "telegram_id",
            "username",
            "first_name",
            "referrer_id",
            "balance",
            "total_earned",
            "reg_date",
            "full_access",
            "is_blocked",
        ]
    )
    for row in rows:
        writer.writerow(row)

    csv_data = output.getvalue()
    output.close()

    file_bytes = io.BytesIO(csv_data.encode("utf-8-sig"))
    filename = f"users_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"

    await bot.send_document(
        call.from_user.id,
        InputFile(file_bytes, filename),
        caption="📤 Экспорт пользователей (CSV)",
    )

    await call.answer("Файл с пользователями отправлен", show_alert=False)

    
@dp.message_handler(commands=["test_signal"])
async def cmd_test_signal(message: types.Message):
    # Только админ
    if not is_admin(message.from_user.id):
        return

    rem = _cooldown_remaining(message.from_user.id, "test_signal", 60)
    if rem > 0:
        await message.answer(f"⏳ Подожди {rem} сек и попробуй ещё раз.")
        return

    await message.answer("⏳ Генерирую тестовый авто-сигнал...")

    text = await build_auto_signal_text(
        AUTO_SIGNALS_SYMBOLS,
        True,  # включено принудительно
    )

    if not text:
        await message.answer("❌ Сейчас нет подходящего сетапа (фильтры не прошли) или CoinGecko временно ограничил запросы (429). Попробуй позже.")
        return

    try:
        msg = await bot.send_message(SIGNALS_CHANNEL_ID, text)
        save_signal_trade(msg.message_id, text)
        await message.answer("✅ Тестовый авто-сигнал отправлен в канал.")
    except Exception as e:
        await message.answer(f"❌ Ошибка при отправке в канал.\nПроверь права бота и ID канала.")
        
@dp.message_handler(commands=["check_binance"])
async def cmd_check_binance(message: types.Message):
    # Только админ
    if not is_admin(message.from_user.id):
        return

    await message.answer("⏳ Проверяю Binance...")

    url = "https://api.binance.com/api/v3/ticker/24hr"
    params = {"symbol": "BTCUSDT"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=10) as resp:
                status = resp.status
                text = await resp.text()
    except Exception as e:
        await message.answer(f"❌ Ошибка при запросе к Binance:\n<code>{e}</code>")
        return

    # Показываем статус и первые символы ответа
    short = text[:600]
    await message.answer(
        f"Статус Binance: <b>{status}</b>\n\n"
        f"Первые символы ответа:\n<code>{short}</code>"
    )

    


def _find_user_by_any(identifier: str):
    conn = db_connect()
    cur = conn.cursor()
    row = None
    if identifier.startswith("@"):
        username = identifier[1:]
        cur.execute("SELECT id, user_id, username, first_name FROM users WHERE username = ?", (username,))
        row = cur.fetchone()
    else:
        try:
            tg_id = int(identifier)
            cur.execute("SELECT id, user_id, username, first_name FROM users WHERE user_id = ?", (tg_id,))
            row = cur.fetchone()
        except ValueError:
            row = None
    conn.close()
    return row


@dp.message_handler(commands=["grant"])
async def cmd_grant(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: <code>/grant @username</code> или <code>/grant 123456789</code>")
        return

    ident = parts[1].strip()
    row = _find_user_by_any(ident)
    if not row:
        await message.answer("Пользователь не найден в базе.")
        return

    user_db_id, tg_id, username, first_name = row
    set_full_access(user_db_id, True)
    extend_signals(user_db_id, days=30)

    await message.answer(f"✅ Полный доступ выдан пользователю @{username if username else tg_id} + сигналы на 30 дней.")
    try:
        await bot.send_message(
            tg_id,
            "🎟 <b>Тебе выдан полный доступ вручную администратором.</b>\n\n"
            "Обучение и партнёрка открыты, сигналы активны на 30 дней.",
        )
    except Exception:
        pass


@dp.message_handler(commands=["extend_signals"])
async def cmd_extend_signals(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Использование: <code>/extend_signals @username</code> или <code>/extend_signals 123456789</code>"
        )
        return

    ident = parts[1].strip()
    row = _find_user_by_any(ident)
    if not row:
        await message.answer("Пользователь не найден в базе.")
        return

    user_db_id, tg_id, username, first_name = row
    extend_signals(user_db_id, days=30)
    await message.answer(f"✅ Сигналы продлены пользователю @{username if username else tg_id} на 30 дней.")
    try:
        await bot.send_message(
            tg_id,
            "📈 <b>Твоя подписка на сигналы продлена администратором ещё на 30 дней.</b>",
        )
    except Exception:
        pass


@dp.message_handler(commands=["user"])
async def cmd_user_info(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: <code>/user @username</code> или <code>/user 123456789</code>")
        return

    ident = parts[1].strip()
    row = _find_user_by_any(ident)
    if not row:
        await message.answer("Пользователь не найден в базе.")
        return

    user_db_id, tg_id, username, first_name = row
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT referrer_id, balance, total_earned, full_access FROM users WHERE id = ?",
        (user_db_id,),
    )
    row2 = cur.fetchone()
    conn.close()
    referrer_id, balance, total_earned, full_access = row2

    lvl1, lvl2 = count_referrals(user_db_id)
    signals_until = get_signals_until(user_db_id)

    text = (
        f"👤 <b>Пользователь</b>\n\n"
        f"ID в БД: <code>{user_db_id}</code>\n"
        f"TG ID: <code>{tg_id}</code>\n"
        f"Username: @{username if username else '—'}\n"
        f"Имя: {first_name}\n\n"
        f"Referrer ID (в БД): {referrer_id}\n"
        f"Full access: {'да' if full_access else 'нет'}\n"
        f"Баланс: {balance}\n"
        f"Всего заработано: {total_earned}\n"
        f"Рефералы: 1л — {lvl1}, 2л — {lvl2}\n"
        f"Сигналы активны до: {signals_until.strftime('%Y-%m-%d %H:%M:%S') if signals_until else 'нет'}"
    )

    await message.answer(text)


# ---------------------------------------------------------------------------
# WATCHER: СЛЕДИМ ЗА ИСТЕЧЕНИЕМ СИГНАЛОВ     
# ---------------------------------------------------------------------------


async def signals_watcher():
    """
    Периодически проверяем, у кого истёк доступ к сигналам,
    и при необходимости кикаем из канала (если у бота есть права).
    """
    await asyncio.sleep(5)
    while True:
        try:
            now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            conn = db_connect()
            cur = conn.cursor()
            cur.execute(
                """
                SELECT sa.user_id, u.user_id
                FROM signals_access sa
                JOIN users u ON sa.user_id = u.id
                WHERE sa.active_until IS NOT NULL AND sa.active_until < ?
                """,
                (now,),
            )
            rows = cur.fetchall()
            conn.close()

            for user_db_id, tg_id in rows:
                try:
                    # мягкий кик: ban + unban, чтобы убрать из канала
                    await bot.ban_chat_member(SIGNALS_CHANNEL_ID, tg_id)
                    await bot.unban_chat_member(SIGNALS_CHANNEL_ID, tg_id)
                    logger.info("Removed user %s from signals channel (expired).", tg_id)
                except Exception as e:
                    logger.error("Failed to remove user %s from channel: %s", tg_id, e)

        except Exception as e:
            logger.error("Signals watcher error: %s", e)
            await notify_admin(f"🚨 signals_watcher error: {e}", key="signals_watcher", cooldown=600)

        await asyncio.sleep(3600)  # раз в час


# ---------------------------------------------------------------------------
# ФОЛЛБЭК
# ---------------------------------------------------------------------------


@dp.message_handler()
async def fallback(message: types.Message):
    if is_spam(message.from_user.id):
        return
    await message.answer(
        "Не понял сообщение 🤔\nИспользуй кнопки внизу или нажми /start, чтобы вернуться в главное меню.",
        reply_markup=main_reply_kb(is_admin=is_admin(message.from_user.id)),
    )



# ---------------------------------------------------------------------------
# ЗАПУСК
# ---------------------------------------------------------------------------


async def on_startup(dp: Dispatcher):
    init_db()

    # Логи старта (в Railway/консоль)
    try:
        me = await bot.get_me()
        logger.info("✅ Bot started: @%s (id=%s)", me.username, me.id)
    except Exception:
        logger.info("✅ Bot started (bot.get_me failed)")

    logger.info("✅ DB: connected (path=%s)", DB_PATH)
    logger.info("✅ Channel: %s", SIGNALS_CHANNEL_ID)
    logger.info(
        "✅ Workers: auto_signals=%s, signals_watcher=ON, tp_monitor=ON",
        "ON" if AUTO_SIGNALS_ENABLED else "OFF",
    )

    # Уведомление админу (редко, чтобы не спамить на каждом рестарте)
    await notify_admin("✅ Бот запущен и воркеры активны.", key="startup", cooldown=900)

    asyncio.create_task(signals_watcher())
    asyncio.create_task(tp_monitor_worker())
    asyncio.create_task(
        auto_signals_worker_tracked(
            bot,
            SIGNALS_CHANNEL_ID,
            AUTO_SIGNALS_PER_DAY,
            AUTO_SIGNALS_SYMBOLS,
            AUTO_SIGNALS_ENABLED,
        )
    )



if __name__ == "__main__":
    init_db()
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)