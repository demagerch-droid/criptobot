# ===================== CONFIG (ВСТАВЬ СВОЁ) =====================
BOT_TOKEN = "PASTE_NEW_BOT_TOKEN_HERE"
ADMIN_ID = 8585550939  # твой Telegram ID (числом)

PRICE_USD = 200.0
SUPPORT_USERNAME = "@TradeX_Partner_helper"         # куда писать по оплате
PRIVATE_GROUP_LINK = "https://t.me/your_private_group"  # ссылка на закрытую группу/чат

REF_L1 = 0.50
REF_L2 = 0.10
MIN_PAYOUT = 10.0
# ================================================================

import os
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, List, Tuple

import aiosqlite
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from contextlib import asynccontextmanager
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("traffic_edu_bot")

if not BOT_TOKEN or "PASTE" in BOT_TOKEN:
    raise RuntimeError("Вставь реальный BOT_TOKEN в начале файла (строкой в кавычках).")

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

# ---- DB PATH (Railway volume recommended) ----
DEFAULT_DB = "/data/database.db"
DB_PATH = DEFAULT_DB
try:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
except Exception:
    DB_PATH = "database.db"

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users(
  user_id INTEGER PRIMARY KEY,
  username TEXT,
  first_name TEXT,
  ref1 INTEGER,
  ref2 INTEGER,
  source TEXT,
  access INTEGER DEFAULT 0,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS payments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  amount REAL,
  status TEXT,          -- pending/approved/rejected
  created_at TEXT,
  decided_at TEXT
);

CREATE TABLE IF NOT EXISTS wallets(
  user_id INTEGER PRIMARY KEY,
  balance REAL DEFAULT 0,
  total_earned REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ref_earnings(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  payment_id INTEGER,
  from_user_id INTEGER,
  to_user_id INTEGER,
  level INTEGER,
  amount REAL,
  created_at TEXT,
  UNIQUE(payment_id, level, to_user_id)
);

CREATE TABLE IF NOT EXISTS progress(
  user_id INTEGER,
  module_id INTEGER,
  lesson_id INTEGER,
  done INTEGER DEFAULT 0,
  updated_at TEXT,
  PRIMARY KEY(user_id, module_id, lesson_id)
);

CREATE TABLE IF NOT EXISTS payouts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  amount REAL,
  details TEXT,
  status TEXT,          -- pending/approved/rejected
  created_at TEXT,
  decided_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_users_ref1 ON users(ref1);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
CREATE INDEX IF NOT EXISTS idx_payouts_status ON payouts(status);
"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()

@asynccontextmanager
async def db_connect():
    # Важно: НЕ делаем "async with await aiosqlite.connect()"
    # Используем контекст-менеджер, чтобы aiosqlite не пытался стартовать поток дважды.
    db = await aiosqlite.connect(DB_PATH, timeout=30)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA busy_timeout=30000;")
    try:
        yield db
    finally:
        await db.close()

# ---------- Anti-copy helper ----------

# Telegram НЕ даёт 100% запретить копирование текста, но:
# 1) protect_content=True запрещает пересылку/сохранение
# 2) невидимый символ U+2060 (WORD JOINER) портит копипаст
def obfuscate_for_copy(text: str) -> str:
    joiner = "\u2060"
    out = []
    for ch in text:
        if ch.isalnum():
            out.append(ch + joiner)
        else:
            out.append(ch)
    return "".join(out)

# ---------- Course content ----------
# Поменяешь тексты как захочешь — структура уже готова.
COURSE: Dict[int, Dict[str, object]] = {
    1: {
        "title": "TikTok база и аккаунты",
        "lessons": [
            (1, "Старт: что такое арбитраж и УБД", "Коротко: арбитраж — это покупка/привлечение трафика и монетизация через офферы.\n\nУБД — модель, где ты даёшь ценность (прогрев/пруфы/кейсы), а потом переводишь в действие (покупка/регистрация/заявка)."),
            (2, "Оформление профиля TikTok под воронку", "Аватар, ник, био, закрепы.\n\nЦель: чтобы за 3 секунды человек понял: кто ты, что даёшь, куда жать дальше."),
            (3, "Аккаунты: прогрев и безопасность", "Не лезь в серые темы с первого дня.\n\nРазгон: контент → активность → плавно CTA.\n\nБазовая безопасность: устройство/симка/поведение."),
        ],
    },
    2: {
        "title": "Контент и креативы",
        "lessons": [
            (1, "Формула креатива на 15 секунд", "Хук (1-2 сек) → проблема → решение → CTA.\n\nСнимай сериями: 20-30 роликов, потом оставляй лучшие."),
            (2, "Идеи хуков, которые реально заходят", "«Я слил 300$ за неделю и понял одну вещь…»\n«Если бы я начинал с нуля — сделал бы так…»\n«3 ошибки, из-за которых ты не льёшь в плюс»"),
            (3, "Тестирование: как понять что держать", "Смотри удержание, досмотры, переходы.\n\nНе женись на одном креативе — тесты решают."),
        ],
    },
    3: {
        "title": "Перелив в Telegram и прогрев",
        "lessons": [
            (1, "Перелив в бота (твоя схема)", "Трафик льём сразу в бота.\n\nЭто хорошо, потому что бот = мини-лендинг + выдача доступа + партнёрка.\n\nКанал ты ведёшь сам (для своих)."),
            (2, "Прогрев внутри бота", "В боте должны быть: выгоды, структура, кейсы/пруфы, FAQ, гарантийные формулировки.\n\nИ один CTA: купить доступ / написать админу."),
            (3, "Как закрывать на оплату", "Снимаешь страх: что внутри, кому подходит, что получит, как быстро применить.\n\nЧёткая цена и понятный шаг оплаты."),
        ],
    },
    4: {
        "title": "Масштабирование и система",
        "lessons": [
            (1, "Система: таблица учёта и дисциплина", "Без учёта ты не масштабируешься.\n\nФиксируй: креатив → дата → метрики → выводы → следующая итерация."),
            (2, "Команда и делегирование", "Сценарии/монтаж/постинг можно делегировать.\n\nТвоя задача: связки + тесты + аналитика."),
            (3, "Финал: доступ в закрытую группу", "В группе будут ветки по модулям, чат, отзывы и обновления.\n\nЖми кнопку ниже и заходи."),
        ],
    },
}

# ---------- Keyboards ----------
def kb_main(access: bool, admin: bool) -> InlineKeyboardMarkup:
    rows = []
    if access:
        rows.append([InlineKeyboardButton(text="📚 Обучение", callback_data="learn")])
        rows.append([InlineKeyboardButton(text="👤 Профиль", callback_data="profile")])
        rows.append([InlineKeyboardButton(text="🎁 Партнёрка", callback_data="partner")])
    else:
        rows.append([InlineKeyboardButton(text="🔥 Что внутри", callback_data="about")])
        rows.append([InlineKeyboardButton(text="💳 Купить доступ ($200)", callback_data="buy")])
        rows.append([InlineKeyboardButton(text="🎁 Партнёрка", callback_data="partner")])

    rows.append([InlineKeyboardButton(text="🆘 Поддержка", url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}")])

    if admin:
        rows.append([InlineKeyboardButton(text="🛠 Админ панель", callback_data="admin")])

    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_back(to: str = "menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=to)],
    ])

def kb_buy() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data="paid")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
    ])

def kb_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Pending оплаты", callback_data="adm:payments")],
        [InlineKeyboardButton(text="💸 Pending выводы", callback_data="adm:payouts")],
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="adm:users")],
        [InlineKeyboardButton(text="🎟 Выдать доступ", callback_data="adm:grant")],
        [InlineKeyboardButton(text="⛔ Отозвать доступ", callback_data="adm:revoke")],
        [InlineKeyboardButton(text="📣 Рассылка", callback_data="adm:broadcast")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")],
    ])

def kb_modules() -> InlineKeyboardMarkup:
    rows = []
    for mid in sorted(COURSE.keys()):
        rows.append([InlineKeyboardButton(text=f"📦 Модуль {mid}: {COURSE[mid]['title']}", callback_data=f"mod:{mid}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_lessons(module_id: int) -> InlineKeyboardMarkup:
    rows = []
    lessons: List[Tuple[int, str, str]] = COURSE[module_id]["lessons"]  # type: ignore
    for lid, title, _ in lessons:
        rows.append([InlineKeyboardButton(text=f"Урок {lid}: {title}", callback_data=f"lesson:{module_id}:{lid}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="learn")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_lesson_nav(module_id: int, lesson_id: int) -> InlineKeyboardMarkup:
    lessons: List[Tuple[int, str, str]] = COURSE[module_id]["lessons"]  # type: ignore
    ids = [lid for (lid, _, _) in lessons]
    i = ids.index(lesson_id)
    prev_id = ids[i - 1] if i > 0 else None
    next_id = ids[i + 1] if i < len(ids) - 1 else None

    rows = []
    nav = []
    if prev_id is not None:
        nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"lesson:{module_id}:{prev_id}"))
    if next_id is not None:
        nav.append(InlineKeyboardButton(text="➡️ Далее", callback_data=f"lesson:{module_id}:{next_id}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="✅ Отметить пройдено", callback_data=f"done:{module_id}:{lesson_id}")])

    if module_id == 4 and lesson_id == 3:
        rows.append([InlineKeyboardButton(text="🚀 В закрытую группу", url=PRIVATE_GROUP_LINK)])

    rows.append([InlineKeyboardButton(text="⬅️ К урокам", callback_data=f"mod:{module_id}")])
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_partner() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 Запросить вывод", callback_data="payout")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
    ])

# ---------- DB helpers ----------
async def upsert_user(user_id: int, username: str, first_name: str, source: str, ref_id: Optional[int]):
    async with db_connect() as db:
        row = await (await db.execute("SELECT user_id, ref1, ref2 FROM users WHERE user_id=?", (user_id,))).fetchone()
        if row:
            # обновим username/first_name при необходимости
            await db.execute("UPDATE users SET username=?, first_name=? WHERE user_id=?", (username, first_name, user_id))
            await db.commit()
            return

        ref1 = None
        ref2 = None
        if ref_id and ref_id != user_id:
            # ref1 = ref_id, ref2 = ref1(ref_id)
            r = await (await db.execute("SELECT ref1 FROM users WHERE user_id=?", (ref_id,))).fetchone()
            ref1 = ref_id
            ref2 = int(r["ref1"]) if r and r["ref1"] else None

        await db.execute(
            "INSERT INTO users(user_id, username, first_name, ref1, ref2, source, access, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (user_id, username, first_name, ref1, ref2, source, 0, now_iso())
        )
        await db.execute("INSERT OR IGNORE INTO wallets(user_id, balance, total_earned) VALUES (?,0,0)", (user_id,))
        await db.commit()

async def get_access(user_id: int) -> bool:
    async with db_connect() as db:
        row = await (await db.execute("SELECT access FROM users WHERE user_id=?", (user_id,))).fetchone()
        return bool(row and row["access"] == 1)

async def set_access(user_id: int, value: bool):
    async with db_connect() as db:
        await db.execute("UPDATE users SET access=? WHERE user_id=?", (1 if value else 0, user_id))
        await db.commit()

async def get_stats(user_id: int) -> Tuple[float, int, int, int]:
    async with db_connect() as db:
        w = await (await db.execute("SELECT balance, total_earned FROM wallets WHERE user_id=?", (user_id,))).fetchone()
        balance = float(w["balance"]) if w else 0.0
        total = float(w["total_earned"]) if w else 0.0

        c1 = await (await db.execute("SELECT COUNT(*) AS c FROM users WHERE ref1=?", (user_id,))).fetchone()
        invited1 = int(c1["c"]) if c1 else 0

        done = await (await db.execute("SELECT COUNT(*) AS c FROM progress WHERE user_id=? AND done=1", (user_id,))).fetchone()
        done_cnt = int(done["c"]) if done else 0

    total_lessons = sum(len(COURSE[mid]["lessons"]) for mid in COURSE)  # type: ignore
    return balance, invited1, done_cnt, total_lessons

async def create_payment_request(user_id: int) -> Optional[int]:
    if await get_access(user_id):
        return None
    async with db_connect() as db:
        pending = await (await db.execute("SELECT id FROM payments WHERE user_id=? AND status='pending' ORDER BY id DESC LIMIT 1", (user_id,))).fetchone()
        if pending:
            return None
        cur = await db.execute(
            "INSERT INTO payments(user_id, amount, status, created_at, decided_at) VALUES (?,?,?,?,?)",
            (user_id, float(PRICE_USD), "pending", now_iso(), None)
        )
        await db.commit()
        return int(cur.lastrowid)

async def get_payment(payment_id: int):
    async with db_connect() as db:
        return await (await db.execute("SELECT * FROM payments WHERE id=?", (payment_id,))).fetchone()

async def list_pending_payments(limit: int = 10):
    async with db_connect() as db:
        return await (await db.execute("SELECT * FROM payments WHERE status='pending' ORDER BY id ASC LIMIT ?", (limit,))).fetchall()

async def decide_payment(payment_id: int, approve: bool) -> Optional[aiosqlite.Row]:
    async with db_connect() as db:
        row = await (await db.execute("SELECT * FROM payments WHERE id=?", (payment_id,))).fetchone()
        if not row or row["status"] != "pending":
            return None
        await db.execute(
            "UPDATE payments SET status=?, decided_at=? WHERE id=?",
            ("approved" if approve else "rejected", now_iso(), payment_id)
        )
        await db.commit()
        return row

async def wallet_add(user_id: int, amount: float):
    async with db_connect() as db:
        await db.execute("INSERT OR IGNORE INTO wallets(user_id, balance, total_earned) VALUES (?,0,0)", (user_id,))
        await db.execute("UPDATE wallets SET balance = balance + ?, total_earned = total_earned + ? WHERE user_id=?", (amount, amount, user_id))
        await db.commit()

async def wallet_sub(user_id: int, amount: float) -> bool:
    async with db_connect() as db:
        w = await (await db.execute("SELECT balance FROM wallets WHERE user_id=?", (user_id,))).fetchone()
        bal = float(w["balance"]) if w else 0.0
        if bal + 1e-9 < amount:
            return False
        await db.execute("UPDATE wallets SET balance = balance - ? WHERE user_id=?", (amount, user_id))
        await db.commit()
        return True

async def apply_ref_earnings(payment_id: int, buyer_id: int, base_amount: float):
    async with db_connect() as db:
        u = await (await db.execute("SELECT ref1, ref2 FROM users WHERE user_id=?", (buyer_id,))).fetchone()
        if not u:
            return
        ref1 = u["ref1"]
        ref2 = u["ref2"]
        created = now_iso()

        if ref1:
            a1 = round(base_amount * REF_L1, 2)
            await db.execute(
                "INSERT OR IGNORE INTO ref_earnings(payment_id, from_user_id, to_user_id, level, amount, created_at) VALUES (?,?,?,?,?,?)",
                (payment_id, buyer_id, int(ref1), 1, a1, created)
            )
            await db.commit()
            await wallet_add(int(ref1), a1)

        if ref2:
            a2 = round(base_amount * REF_L2, 2)
            await db.execute(
                "INSERT OR IGNORE INTO ref_earnings(payment_id, from_user_id, to_user_id, level, amount, created_at) VALUES (?,?,?,?,?,?)",
                (payment_id, buyer_id, int(ref2), 2, a2, created)
            )
            await db.commit()
            await wallet_add(int(ref2), a2)

async def progress_done(user_id: int, module_id: int, lesson_id: int):
    async with db_connect() as db:
        await db.execute(
            "INSERT INTO progress(user_id, module_id, lesson_id, done, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(user_id, module_id, lesson_id) DO UPDATE SET done=1, updated_at=excluded.updated_at",
            (user_id, module_id, lesson_id, 1, now_iso())
        )
        await db.commit()

# ---------- Payouts ----------
async def create_payout(user_id: int, amount: float, details: str) -> int:
    async with db_connect() as db:
        cur = await db.execute(
            "INSERT INTO payouts(user_id, amount, details, status, created_at, decided_at) VALUES (?,?,?,?,?,?)",
            (user_id, amount, details, "pending", now_iso(), None)
        )
        await db.commit()
        return int(cur.lastrowid)

async def list_pending_payouts(limit: int = 10):
    async with db_connect() as db:
        return await (await db.execute("SELECT * FROM payouts WHERE status='pending' ORDER BY id ASC LIMIT ?", (limit,))).fetchall()

async def decide_payout(payout_id: int, approve: bool) -> Optional[aiosqlite.Row]:
    async with db_connect() as db:
        row = await (await db.execute("SELECT * FROM payouts WHERE id=?", (payout_id,))).fetchone()
        if not row or row["status"] != "pending":
            return None
        await db.execute(
            "UPDATE payouts SET status=?, decided_at=? WHERE id=?",
            ("approved" if approve else "rejected", now_iso(), payout_id)
        )
        await db.commit()
        return row

# ---------- FSM ----------
class AdminFSM(StatesGroup):
    grant = State()
    revoke = State()
    broadcast = State()

class PayoutFSM(StatesGroup):
    amount = State()
    details = State()

# ---------- Bot / Routers ----------
bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML, protect_content=True)
)
dp = Dispatcher(storage=MemoryStorage())
r = Router()
dp.include_router(r)

async def safe_edit(call: CallbackQuery, text: str, kb: Optional[InlineKeyboardMarkup] = None):
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)

# ---------- Handlers ----------
@r.message(CommandStart())
async def on_start(message: Message):
    payload = ""
    if message.text and len(message.text.split(maxsplit=1)) == 2:
        payload = message.text.split(maxsplit=1)[1].strip()

    ref_id = None
    source = ""
    if payload.startswith("ref_"):
        try:
            ref_id = int(payload.replace("ref_", "").strip())
        except Exception:
            ref_id = None
    elif payload.startswith("src_"):
        source = payload.replace("src_", "")[:32]

    await upsert_user(
        user_id=message.from_user.id,
        username=message.from_user.username or "",
        first_name=message.from_user.first_name or "",
        source=source,
        ref_id=ref_id
    )

    access = await get_access(message.from_user.id)
    await message.answer(
        "👋 <b>Traffic Partner Bot</b>\n\n"
        "Обучение по арбитражу трафика (TikTok) + партнёрка.\n\n"
        "Выбирай действие 👇",
        reply_markup=kb_main(access, is_admin(message.from_user.id)),
    )

@r.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery):
    access = await get_access(call.from_user.id)
    await safe_edit(
        call,
        "🏠 <b>Меню</b>",
        kb_main(access, is_admin(call.from_user.id))
    )
    await call.answer()

@r.callback_query(F.data == "about")
async def cb_about(call: CallbackQuery):
    text = (
        "🔥 <b>Что внутри</b>\n\n"
        "Ты получишь пошаговую систему арбитража с TikTok:\n"
        "• аккаунты и безопасность\n"
        "• креативы и тесты\n"
        "• перелив в Telegram и прогрев\n"
        "• масштабирование и система\n\n"
        f"💳 Доступ навсегда: <b>${PRICE_USD:.0f}</b>\n"
        f"💬 По оплате: {SUPPORT_USERNAME}\n\n"
        "После оплаты кнопка покупки исчезнет и откроется обучение ✅"
    )
    await safe_edit(call, text, InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Купить доступ", callback_data="buy")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
    ]))
    await call.answer()

@r.callback_query(F.data == "buy")
async def cb_buy(call: CallbackQuery):
    if await get_access(call.from_user.id):
        await call.answer("У тебя уже есть доступ ✅", show_alert=True)
        return

    text = (
        "💳 <b>Покупка доступа</b>\n\n"
        f"Цена: <b>${PRICE_USD:.0f}</b>\n"
        "Доступ: <b>навсегда</b>\n\n"
        f"1) Напиши админу: {SUPPORT_USERNAME}\n"
        "2) Оплати\n"
        "3) Вернись сюда и нажми «Я оплатил»\n\n"
        "Админ подтвердит — доступ откроется автоматически ✅"
    )
    await safe_edit(call, text, kb_buy())
    await call.answer()

@r.callback_query(F.data == "paid")
async def cb_paid(call: CallbackQuery):
    pid = await create_payment_request(call.from_user.id)
    if pid is None:
        await call.answer("Заявка уже есть или доступ уже активен ✅", show_alert=True)
        return

    # notify admin
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Approve", callback_data=f"adm_pay:ok:{pid}"),
            InlineKeyboardButton(text="❌ Reject", callback_data=f"adm_pay:no:{pid}")
        ]
    ])
    try:
        await bot.send_message(
            ADMIN_ID,
            f"💳 <b>Новая заявка на оплату</b>\n"
            f"Payment ID: <code>{pid}</code>\n"
            f"User: <code>{call.from_user.id}</code> (@{call.from_user.username or '—'})\n"
            f"Amount: <b>${PRICE_USD:.0f}</b>",
            reply_markup=kb,
            protect_content=False
        )
    except Exception as e:
        log.warning("Cannot notify admin: %s", e)

    await safe_edit(
        call,
        "⏳ Заявка отправлена админу. После подтверждения доступ откроется автоматически.",
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 Меню", callback_data="menu")]])
    )
    await call.answer()

@r.callback_query(F.data.startswith("adm_pay:"))
async def cb_admin_payment(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return

    _, decision, pid_s = call.data.split(":")
    pid = int(pid_s)
    approve = decision == "ok"

    row = await decide_payment(pid, approve)
    if not row:
        await call.answer("Уже обработано / не найдено", show_alert=True)
        return

    buyer_id = int(row["user_id"])
    buyer_had_access = await get_access(buyer_id)

    if approve:
        # выдаём доступ
        await set_access(buyer_id, True)

        # начисляем партнёрку только если доступ был неактивен до подтверждения
        if not buyer_had_access:
            await apply_ref_earnings(pid, buyer_id, base_amount=float(PRICE_USD))

        # уведомим покупателя
        try:
            await bot.send_message(
                buyer_id,
                "✅ <b>Оплата подтверждена!</b>\n\n"
                "Доступ открыт <b>навсегда</b>. Заходи в «📚 Обучение».",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 Меню", callback_data="menu")]])
            )
        except Exception:
            pass

        await safe_edit(call, f"✅ Approved payment <code>{pid}</code>", kb_admin())
    else:
        try:
            await bot.send_message(buyer_id, "❌ Оплата отклонена. Напиши в поддержку.")
        except Exception:
            pass
        await safe_edit(call, f"❌ Rejected payment <code>{pid}</code>", kb_admin())

    await call.answer()

@r.callback_query(F.data == "learn")
async def cb_learn(call: CallbackQuery):
    if not await get_access(call.from_user.id):
        await call.answer("Сначала купи доступ 💳", show_alert=True)
        return
    await safe_edit(call, "📚 <b>Обучение</b>\nВыбери модуль 👇", kb_modules())
    await call.answer()

@r.callback_query(F.data.startswith("mod:"))
async def cb_mod(call: CallbackQuery):
    if not await get_access(call.from_user.id):
        await call.answer("Нет доступа 💳", show_alert=True)
        return
    module_id = int(call.data.split(":", 1)[1])
    await safe_edit(call, f"📦 <b>Модуль {module_id}</b>: {COURSE[module_id]['title']}", kb_lessons(module_id))
    await call.answer()

@r.callback_query(F.data.startswith("lesson:"))
async def cb_lesson(call: CallbackQuery):
    if not await get_access(call.from_user.id):
        await call.answer("Нет доступа 💳", show_alert=True)
        return
    _, m, l = call.data.split(":")
    module_id = int(m)
    lesson_id = int(l)

    lessons: List[Tuple[int, str, str]] = COURSE[module_id]["lessons"]  # type: ignore
    match = [x for x in lessons if x[0] == lesson_id]
    if not match:
        await call.answer("Урок не найден", show_alert=True)
        return
    _, title, body = match[0]

    # анти-копипаст только для тела урока
    safe_body = obfuscate_for_copy(body)

    text = (
        f"📘 <b>Модуль {module_id} • Урок {lesson_id}</b>\n"
        f"<b>{title}</b>\n\n"
        f"{safe_body}"
    )
    await safe_edit(call, text, kb_lesson_nav(module_id, lesson_id))
    await call.answer()

@r.callback_query(F.data.startswith("done:"))
async def cb_done(call: CallbackQuery):
    if not await get_access(call.from_user.id):
        await call.answer("Нет доступа 💳", show_alert=True)
        return
    _, m, l = call.data.split(":")
    module_id = int(m)
    lesson_id = int(l)
    await progress_done(call.from_user.id, module_id, lesson_id)
    await call.answer("Отмечено ✅", show_alert=True)

@r.callback_query(F.data == "profile")
async def cb_profile(call: CallbackQuery):
    access = await get_access(call.from_user.id)
    balance, invited1, done_cnt, total_lessons = await get_stats(call.from_user.id)

    text = (
        "👤 <b>Профиль</b>\n\n"
        f"ID: <code>{call.from_user.id}</code>\n"
        f"Статус: {'✅ доступ активен' if access else '❌ нет доступа'}\n"
        f"Прогресс: <b>{done_cnt}/{total_lessons}</b>\n\n"
        f"Баланс партнёрки: <b>${balance:.2f}</b>\n"
        f"Рефералы 1 уровня: <b>{invited1}</b>\n"
    )
    await safe_edit(call, text, InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Партнёрка", callback_data="partner")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
    ]))
    await call.answer()

@r.callback_query(F.data == "partner")
async def cb_partner(call: CallbackQuery):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{call.from_user.id}"

    access = await get_access(call.from_user.id)
    balance, invited1, _, _ = await get_stats(call.from_user.id)

    text = (
        "🎁 <b>Партнёрка</b>\n\n"
        "Начисления:\n"
        f"• 1 уровень: <b>{int(REF_L1*100)}%</b>\n"
        f"• 2 уровень: <b>{int(REF_L2*100)}%</b>\n\n"
        f"Баланс: <b>${balance:.2f}</b>\n"
        f"Рефералы (1 уровень): <b>{invited1}</b>\n\n"
        f"🔗 Твоя реф-ссылка:\n<code>{link}</code>\n\n"
        + ("✅ Вывод доступен." if access else "ℹ️ Рекомендуется купить доступ, чтобы пользоваться всем функционалом.")
    )
    await safe_edit(call, text, kb_partner())
    await call.answer()

# ---------- Payout flow ----------
@r.callback_query(F.data == "payout")
async def cb_payout(call: CallbackQuery, state: FSMContext):
    if not await get_access(call.from_user.id):
        await call.answer("Вывод доступен после покупки доступа 💳", show_alert=True)
        return
    balance, _, _, _ = await get_stats(call.from_user.id)
    await state.set_state(PayoutFSM.amount)
    await safe_edit(
        call,
        "💸 <b>Запрос вывода</b>\n\n"
        f"Баланс: <b>${balance:.2f}</b>\n"
        f"Минималка: <b>${MIN_PAYOUT:.2f}</b>\n\n"
        "Введи сумму числом (например 25 или 25.5):",
        kb_back("partner")
    )
    await call.answer()

@r.message(PayoutFSM.amount)
async def payout_amount(message: Message, state: FSMContext):
    try:
        amount = float((message.text or "").replace(",", ".").strip())
    except Exception:
        await message.answer("Введи число (пример: 25 или 25.5).")
        return

    if amount < MIN_PAYOUT:
        await message.answer(f"Минималка: ${MIN_PAYOUT:.2f}")
        return

    balance, *_ = await get_stats(message.from_user.id)
    if balance + 1e-9 < amount:
        await message.answer(f"Недостаточно средств. Баланс: ${balance:.2f}")
        return

    await state.update_data(amount=amount)
    await state.set_state(PayoutFSM.details)
    await message.answer(
        "Ок ✅\nТеперь отправь реквизиты для вывода (карта/USDT TRC20 и т.д.):\n\n"
        "Пример: USDT TRC20: Txxxx..."
    )

@r.message(PayoutFSM.details)
async def payout_details(message: Message, state: FSMContext):
    data = await state.get_data()
    amount = float(data["amount"])
    details = (message.text or "").strip()
    if len(details) < 5:
        await message.answer("Реквизиты слишком короткие. Отправь нормально.")
        return

    # списываем сразу, чтобы не было двойных заявок
    ok = await wallet_sub(message.from_user.id, amount)
    if not ok:
        bal, *_ = await get_stats(message.from_user.id)
        await message.answer(f"Недостаточно средств. Баланс: ${bal:.2f}")
        await state.clear()
        return

    rid = await create_payout(message.from_user.id, amount, details)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Approve", callback_data=f"adm_out:ok:{rid}"),
            InlineKeyboardButton(text="❌ Reject", callback_data=f"adm_out:no:{rid}")
        ]
    ])
    try:
        await bot.send_message(
            ADMIN_ID,
            f"💸 <b>Заявка на вывод</b>\n"
            f"ID: <code>{rid}</code>\n"
            f"User: <code>{message.from_user.id}</code>\n"
            f"Amount: <b>${amount:.2f}</b>\n"
            f"Details: <code>{details}</code>",
            reply_markup=kb,
            protect_content=False
        )
    except Exception:
        pass

    await message.answer("✅ Заявка создана. Админ проверит и подтвердит.")
    await state.clear()

@r.callback_query(F.data.startswith("adm_out:"))
async def cb_admin_payout(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    _, decision, rid_s = call.data.split(":")
    rid = int(rid_s)
    approve = decision == "ok"

    row = await decide_payout(rid, approve)
    if not row:
        await call.answer("Уже обработано / не найдено", show_alert=True)
        return

    uid = int(row["user_id"])
    amount = float(row["amount"])

    if approve:
        try:
            await bot.send_message(uid, f"✅ Вывод подтверждён: ${amount:.2f}")
        except Exception:
            pass
        await safe_edit(call, f"✅ Approved payout <code>{rid}</code>", kb_admin())
    else:
        # refund
        await wallet_add(uid, amount)
        try:
            await bot.send_message(uid, f"❌ Вывод отклонён. Сумма возвращена на баланс: ${amount:.2f}")
        except Exception:
            pass
        await safe_edit(call, f"❌ Rejected payout <code>{rid}</code> (refunded)", kb_admin())

    await call.answer()

# ---------- Admin panel ----------
@r.callback_query(F.data == "admin")
async def cb_admin(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await safe_edit(call, "🛠 <b>Админ панель</b>", kb_admin())
    await call.answer()

@r.callback_query(F.data.startswith("adm:"))
async def cb_adm_actions(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return

    action = call.data.split(":", 1)[1]

    if action == "payments":
        rows = await list_pending_payments(10)
        if not rows:
            await safe_edit(call, "✅ Pending оплат нет.", kb_admin())
            await call.answer()
            return
        lines = ["💳 <b>Pending оплаты</b>\n"]
        kb_rows = []
        for row in rows:
            pid = int(row["id"])
            uid = int(row["user_id"])
            lines.append(f"• <code>{pid}</code> | user <code>{uid}</code> | ${float(row['amount']):.0f}")
            kb_rows.append([
                InlineKeyboardButton(text=f"✅ #{pid}", callback_data=f"adm_pay:ok:{pid}"),
                InlineKeyboardButton(text=f"❌ #{pid}", callback_data=f"adm_pay:no:{pid}")
            ])
        kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin")])
        await safe_edit(call, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows))
        await call.answer()
        return

    if action == "payouts":
        rows = await list_pending_payouts(10)
        if not rows:
            await safe_edit(call, "✅ Pending выводов нет.", kb_admin())
            await call.answer()
            return
        lines = ["💸 <b>Pending выводы</b>\n"]
        kb_rows = []
        for row in rows:
            rid = int(row["id"])
            uid = int(row["user_id"])
            amount = float(row["amount"])
            lines.append(f"• <code>{rid}</code> | user <code>{uid}</code> | ${amount:.2f}")
            kb_rows.append([
                InlineKeyboardButton(text=f"✅ #{rid}", callback_data=f"adm_out:ok:{rid}"),
                InlineKeyboardButton(text=f"❌ #{rid}", callback_data=f"adm_out:no:{rid}")
            ])
        kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin")])
        await safe_edit(call, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows))
        await call.answer()
        return

    if action == "users":
        async with db_connect() as db:
            rows = await (await db.execute(
                "SELECT user_id, username, access, created_at FROM users ORDER BY created_at DESC LIMIT 30"
            )).fetchall()
        if not rows:
            await safe_edit(call, "Пользователей нет.", kb_admin())
            await call.answer()
            return
        lines = ["👥 <b>Последние пользователи</b>\n"]
        for u in rows:
            acc = "✅" if int(u["access"]) == 1 else "❌"
            uname = f"@{u['username']}" if u["username"] else "—"
            lines.append(f"{acc} <code>{u['user_id']}</code> {uname}")
        await safe_edit(call, "\n".join(lines), kb_admin())
        await call.answer()
        return

    if action == "grant":
        await state.set_state(AdminFSM.grant)
        await safe_edit(call, "🎟 Введи Telegram ID пользователя (цифры), кому выдать доступ:", kb_back("admin"))
        await call.answer()
        return

    if action == "revoke":
        await state.set_state(AdminFSM.revoke)
        await safe_edit(call, "⛔ Введи Telegram ID пользователя (цифры), у кого отозвать доступ:", kb_back("admin"))
        await call.answer()
        return

    if action == "broadcast":
        await state.set_state(AdminFSM.broadcast)
        await safe_edit(call, "📣 Отправь текст рассылки одним сообщением:", kb_back("admin"))
        await call.answer()
        return

    await call.answer()

@r.message(AdminFSM.grant)
async def admin_grant(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if not (message.text or "").strip().isdigit():
        await message.answer("Нужно число (Telegram ID).")
        return
    uid = int(message.text.strip())
    await set_access(uid, True)
    try:
        await bot.send_message(uid, "✅ Админ выдал доступ навсегда.", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🏠 Меню", callback_data="menu")]]
        ))
    except Exception:
        pass
    await message.answer("Готово ✅", reply_markup=kb_admin())
    await state.clear()

@r.message(AdminFSM.revoke)
async def admin_revoke(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if not (message.text or "").strip().isdigit():
        await message.answer("Нужно число (Telegram ID).")
        return
    uid = int(message.text.strip())
    await set_access(uid, False)
    try:
        await bot.send_message(uid, "⛔ Доступ отозван админом.")
    except Exception:
        pass
    await message.answer("Готово ⛔", reply_markup=kb_admin())
    await state.clear()

@r.message(AdminFSM.broadcast)
async def admin_broadcast(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    text = (message.text or "").strip()
    if len(text) < 3:
        await message.answer("Слишком короткий текст.")
        return

    async with db_connect() as db:
        rows = await (await db.execute("SELECT user_id FROM users")).fetchall()
    user_ids = [int(r["user_id"]) for r in rows]

    sent = 0
    failed = 0
    for uid in user_ids:
        try:
            await bot.send_message(uid, text, protect_content=False)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.03)

    await message.answer(f"Рассылка завершена ✅\nSent: {sent}\nFailed: {failed}", reply_markup=kb_admin())
    await state.clear()

# Commands as backup
@r.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        return
    access = await get_access(message.from_user.id)
    await message.answer("🛠 <b>Админ панель</b>", reply_markup=kb_admin())

async def main():
    await init_db()
    me = await bot.get_me()
    log.info("Bot started as @%s | db=%s", me.username, DB_PATH)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
