import logging
import sqlite3
from datetime import datetime, timedelta
import asyncio
import random

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
# НАСТРОЙКИ И ПЕРЕМЕННЫЕ
# ---------------------------------------------------------------------------

# ТВОИ ДАННЫЕ
BOT_TOKEN = "8330326273:AAEuWSwkqi7ypz1LZL4LXRr2jSMpKjGc36k"
ADMIN_ID = 682938643
TRONGRID_API_KEY = "b33b8d65-10c9-47fb-99e0-ab47f3bbbb60"
WALLET_ADDRESS = "TSY9xF24bQ3Kbdi1Npj2w4pEEoqJow1nfpr"
CHANNEL_ID = -1003464806734  # канал с сигналами
SUPPORT_CONTACT = "@support"  # логин поддержки, поменяешь под себя

# ЦЕНЫ
PRICE_PACKAGE = 100  # первый платёж: обучение + 1 месяц сигналов + партнёрка
PRICE_RENEWAL = 50   # продление сигналов на месяц (без реф.начислений)

# ПАРТНЁРКА
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

    # пользователи
    cur.execute("""
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
    """)

    # покупки (пакет / продление)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            product_code TEXT, -- 'package' или 'renewal'
            amount REAL,
            status TEXT,
            created_at TEXT,
            paid_at TEXT,
            tx_id TEXT
        )
    """)

    # прогресс по курсам: crypto / traffic
    cur.execute("""
        CREATE TABLE IF NOT EXISTS progress_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            course TEXT,
            module_key TEXT,
            lesson_index INTEGER,
            UNIQUE(user_id, course)
        )
    """)

    # доступ к сигналам
    cur.execute("""
        CREATE TABLE IF NOT EXISTS signals_access (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,       -- id из таблицы users
            active_until TEXT      -- дата до которой есть доступ
        )
    """)

    conn.commit()
    conn.close()


# ---------- USERS ----------

def get_or_create_user(message: types.Message, referrer_id: int = None) -> int:
    user_id = message.from_user.id
    username = message.from_user.username or ""
    first_name = message.from_user.first_name or ""

    conn = db_connect()
    cur = conn.cursor()

    cur.execute("SELECT id FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()

    if row:
        user_db_id = row[0]
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


def get_user_by_tg(user_id: int):
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


# ---------- PURCHASES / ПАКЕТЫ ----------

def create_purchase(user_db_id: int, product_code: str, base_price: float) -> int:
    """
    Создаём покупку с уникальной дробной частью, чтобы проще сверять оплату.
    """
    unique_tail = random.randint(11, 987)  # 0.011 .. 0.987
    amount = base_price + unique_tail / 1000.0

    conn = db_connect()
    cur = conn.cursor()
    created_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        "INSERT INTO purchases (user_id, product_code, amount, status, created_at) "
        "VALUES (?, ?, ?, 'pending', ?)",
        (user_db_id, product_code, amount, created_at),
    )
    conn.commit()
    purchase_id = cur.lastrowid
    conn.close()
    return purchase_id


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


def has_paid_package(user_db_id: int) -> bool:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM purchases WHERE user_id = ? AND product_code = 'package' AND status = 'paid' LIMIT 1",
        (user_db_id,),
    )
    row = cur.fetchone()
    conn.close()
    return bool(row)


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


# ---------- ПРОГРЕСС ПО КУРСАМ ----------

def set_progress(user_id: int, course: str, module_key: str, lesson_index: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM progress_new WHERE user_id = ? AND course = ?",
        (user_id, course),
    )
    row = cur.fetchone()
    if row:
        cur.execute(
            "UPDATE progress_new SET module_key = ?, lesson_index = ? WHERE id = ?",
            (module_key, lesson_index, row[0]),
        )
    else:
        cur.execute(
            "INSERT INTO progress_new (user_id, course, module_key, lesson_index) VALUES (?, ?, ?, ?)",
            (user_id, course, module_key, lesson_index),
        )
    conn.commit()
    conn.close()


def get_progress(user_id: int, course: str):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT module_key, lesson_index FROM progress_new WHERE user_id = ? AND course = ?",
        (user_id, course),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None, 0
    return row[0], row[1]


# ---------- СИГНАЛЫ (ДОСТУП) ----------

def get_signals_until(user_db_id: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT active_until FROM signals_access WHERE user_id = ?",
        (user_db_id,),
    )
    row = cur.fetchone()
    conn.close()
    if not row or not row[0]:
        return None
    try:
        return datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def set_signals_until(user_db_id: int, until: datetime):
    until_str = until.strftime("%Y-%m-%d %H:%M:%S")
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT id FROM signals_access WHERE user_id = ?", (user_db_id,))
    row = cur.fetchone()
    if row:
        cur.execute(
            "UPDATE signals_access SET active_until = ? WHERE id = ?",
            (until_str, row[0]),
        )
    else:
        cur.execute(
            "INSERT INTO signals_access (user_id, active_until) VALUES (?, ?)",
            (user_db_id, until_str),
        )
    conn.commit()
    conn.close()


def extend_signals(user_db_id: int, days: int = 30):
    now = datetime.utcnow()
    current_until = get_signals_until(user_db_id)
    if current_until and current_until > now:
        base = current_until
    else:
        base = now
    new_until = base + timedelta(days=days)
    set_signals_until(user_db_id, new_until)
    return new_until


# ---------------------------------------------------------------------------
# АНТИСПАМ
# ---------------------------------------------------------------------------

user_last_action = {}
ANTISPAM_SECONDS = 1.2


def is_spam(user_id: int) -> bool:
    now = datetime.utcnow()
    last = user_last_action.get(user_id)
    user_last_action[user_id] = now
    if not last:
        return False
    return (now - last) < timedelta(seconds=ANTISPAM_SECONDS)


# ---------------------------------------------------------------------------
# КУРСЫ: КРИПТА И ПЕРЕЛИВ
# ---------------------------------------------------------------------------

COURSE_CRYPTO = {
    "c1_mindset": (
        "Модуль 1. Психология и основы крипторынка",
        [
            "💡 <b>Урок 1. Как здесь реально зарабатывают</b>\n\n"
            "Крипта — это не казино и не волшебная кнопка удвоения депозита. "
            "Здесь зарабатывают те, кто понимает рынок, работает по системе и держит себя в руках.\n\n"
            "Твоя задача — перестать «ставить» и начать <b>торговать</b>.",

            "💡 <b>Урок 2. Трейдер vs инвестор</b>\n\n"
            "Трейдер:\n"
            "• держит сделку от минут до дней;\n"
            "• управляет риском в каждой позиции;\n"
            "• мыслит сериями сделок.\n\n"
            "Инвестор:\n"
            "• покупает монету в долгую;\n"
            "• переносит большие просадки;\n"
            "• смотрит на фундаментал.\n\n"
            "Здесь мы развиваем в тебе именно трейдера, а не случайного игрока.",

            "💡 <b>Урок 3. Как устроена биржа</b>\n\n"
            "Биржа — это место, где встречаются ордера покупателей и продавцов.\n"
            "Есть ордербук, лимитные и рыночные ордера, спред и ликвидность.\n\n"
            "Чем больше ликвидность — тем проще войти и выйти без сильного проскальзывания.",

            "💡 <b>Урок 4. Психологические ловушки</b>\n\n"
            "Главные враги трейдера:\n"
            "• FOMO — страх упустить движение;\n"
            "• жадность — «ещё посижу»;\n"
            "• желание отыграться после серии минусов;\n"
            "• эго — «рынок обязан развернуться».\n\n"
            "Мы будем строить систему так, чтобы эти эмоции не убивали депозит.",
        ],
    ),

    "c2_risk": (
        "Модуль 2. Риск-менеджмент и управление депозитом",
        [
            "📊 <b>Урок 1. Риск на сделку</b>\n\n"
            "Базовое правило: риск 1–2% от депозита на одну сделку.\n\n"
            "Депозит 1000$ → 1% = 10$. Это максимум, который ты готов потерять в одной сделке "
            "без истерик и желания «отыграться».",

            "📊 <b>Урок 2. Как считать объём позиции</b>\n\n"
            "Алгоритм:\n"
            "1) Определи вход и стоп-лосс.\n"
            "2) Посчитай размер стопа в %.\n"
            "3) Реши, сколько % депозита готов рискнуть (например, 1%).\n"
            "4) Риск в $ / стоп в % = объём позиции.\n\n"
            "Пример: депозит 500$, риск 1% (5$), стоп 4% → 5 / 0.04 = 125$ объём позиции.",

            "📊 <b>Урок 3. Соотношение риск / прибыль</b>\n\n"
            "Каждая сделка должна иметь R:R не хуже 1:2.\n"
            "Рискуешь 10$, потенциальная прибыль минимум 20$.\n\n"
            "Тогда даже при 40–50% прибыльных сделок ты будешь в плюсе на дистанции.",

            "📊 <b>Урок 4. Серии сделок и просадки</b>\n\n"
            "Слив происходит не из-за одной сделки, а из-за серии решений.\n"
            "Нормально иметь серию стопов. Ненормально — после серии увеличивать риск.\n\n"
            "Твоя цель — переживать плохие участки рынка без потери всего депозита.",
        ],
    ),

    "c3_tech": (
        "Модуль 3. Технический анализ без воды",
        [
            "📈 <b>Урок 1. Тренд и флэт</b>\n\n"
            "Бычий тренд — последовательность более высоких максимумов и минимумов.\n"
            "Медвежий тренд — наоборот.\n"
            "Флэт — когда цена стоит в диапазоне.\n\n"
            "Сначала определяем, есть ли вообще тренд, а потом уже ищем точки входа.",

            "📈 <b>Урок 2. Уровни и зоны интереса</b>\n\n"
            "Уровни — зоны, где цена уже реагировала: разворотами или остановками.\n"
            "Чем больше касаний, тем уровень сильнее.\n\n"
            "Мы используем уровни как ориентиры для входа, стопа и целей.",

            "📈 <b>Урок 3. Таймфреймы</b>\n\n"
            "Старший ТФ показывает общую картину, младший — точку входа.\n"
            "Например: тренд смотрим на 4H, вход ищем на 15m.\n\n"
            "Не имеет смысла ловить разворот на минутках против мощного дневного тренда.",
        ],
    ),

    "c4_system": (
        "Модуль 4. Торговая система и журнал",
        [
            "🧩 <b>Урок 1. Состав стратегии</b>\n\n"
            "В любой рабочей системе есть:\n"
            "• условия входа;\n"
            "• место стопа;\n"
            "• правила выхода в плюс;\n"
            "• размер риска;\n"
            "• время, когда ты торгуешь.\n\n"
            "Если чего-то нет — это уже не стратегия.",

            "🧩 <b>Урок 2. Базовый сетап по тренду</b>\n\n"
            "1) Определяем тренд на старшем ТФ.\n"
            "2) Ждём откат к зоне интереса.\n"
            "3) На младшем ТФ ждём подтверждение входа.\n"
            "4) Стоп — за уровень, цель — ближайшая сильная зона.\n\n"
            "Одна простая схема, которую можно повторять много раз.",

            "🧩 <b>Урок 3. Журнал сделок</b>\n\n"
            "Записывай каждую сделку: вход, стоп, цель, риск, результат и комментарий.\n"
            "Раз в неделю смотри журнал и отмечай повторяющиеся ошибки.\n\n"
            "Без журнала ты будешь наступать на одни и те же грабли.",
        ],
    ),
}

COURSE_TRAFFIC = {
    "t1_profile": (
        "Модуль 1. Позиционирование и профиль",
        [
            "🚀 <b>Урок 1. Зачем тебе TikTok</b>\n\n"
            "TikTok — это витрина. Задача: привести людей в бота, где они получают обучение, сигналы и партнёрку.\n\n"
            "Каждый ролик — это приглашение в твою систему, а не просто развлечение.",

            "🚀 <b>Урок 2. Оформление профиля</b>\n\n"
            "Профиль должен за пару секунд объяснять, чем ты полезен:\n"
            "• аватар с ассоциацией денег/крипты;\n"
            "• описание, кто ты и чем занимаешься;\n"
            "• призыв: «Обучение и закрытый канал — ссылка в профиле».\n\n"
            "Слабый профиль = потерянный трафик.",

            "🚀 <b>Урок 3. Триггеры доверия</b>\n\n"
            "Люди чаще переходят в бота, когда видят:\n"
            "• честные истории и твой путь;\n"
            "• разбор ошибок новичков;\n"
            "• адекватное отношение к рискам.\n\n"
            "Добавляй такие элементы в контент — это сильно поднимает конверсию.",
        ],
    ),

    "t2_content": (
        "Модуль 2. Контент, который приводит людей",
        [
            "🎥 <b>Урок 1. Структура ролика</b>\n\n"
            "Рабочая схема:\n"
            "1) крючок в первые секунды (боль/вопрос/сильная фраза);\n"
            "2) короткое объяснение идеи;\n"
            "3) пример или мини-история;\n"
            "4) призыв перейти в бота по ссылке в профиле.\n\n"
            "Без призыва люди просто листают дальше.",

            "🎥 <b>Урок 2. Темы для роликов</b>\n\n"
            "• ошибки новичков в крипте;\n"
            "• реальные истории заработка/слива;\n"
            "• объяснение, чем трейдинг отличается от казино;\n"
            "• как можно отбить 100$ через партнёрку.\n\n"
            "Каждый ролик подводит к боту и даёт логичный шаг дальше.",

            "🎥 <b>Урок 3. Регулярность и план</b>\n\n"
            "Один ролик в день стабильно лучше, чем 10 роликов раз в неделю.\n"
            "Составь список тем на неделю и снимай партиями.\n\n"
            "Системный контент = системный трафик.",
        ],
    ),

    "t3_funnel": (
        "Модуль 3. Воронка: от ролика до оплаты",
        [
            "📲 <b>Урок 1. Путь пользователя</b>\n\n"
            "Путь простой:\n"
            "TikTok → профиль → ссылка → бот → /start → обучение и оффер на 100$.\n\n"
            "Наша задача — сделать этот путь понятным даже для новичка.",

            "📲 <b>Урок 2. Что человек получает за 100$</b>\n\n"
            "Человек должен чётко понимать, за что он платит:\n"
            "• обучение по крипте внутри бота;\n"
            "• доступ к сигналам на месяц;\n"
            "• партнёрку 50% / 10%.\n\n"
            "Плюс — он может отбить вложение, приведя всего пару человек.",

            "📲 <b>Урок 3. На чём ты здесь зарабатываешь</b>\n\n"
            "Ты не просто «продаёшь курс». Ты строишь систему, где:\n"
            "• люди получают реальный продукт;\n"
            "• могут зарабатывать по партнёрке;\n"
            "• ты зарабатываешь вместе с ними.\n\n"
            "Продления сигналов по 50$ идут на поддержку канала — с них реф. бонусы не начисляются.",
        ],
    ),
}

# ---------------------------------------------------------------------------
# КЛАВИАТУРЫ
# ---------------------------------------------------------------------------


def main_menu():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton("📚 Обучение по крипте"))
    kb.row(KeyboardButton("🚀 Обучение по переливу трафика"))
    kb.row(
        KeyboardButton("📈 Сигналы по торговле"),
        KeyboardButton("💼 Комбо: обучение + сигналы"),
    )
    kb.row(
        KeyboardButton("👥 Партнёрская программа"),
        KeyboardButton("📊 Моя статистика"),
    )
    kb.row(KeyboardButton("📩 Поддержка"))
    return kb


def training_menu_keyboard(course: str):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("▶️ Продолжить обучение", callback_data=f"train_start:{course}"))
    kb.add(InlineKeyboardButton("📚 Структура курса", callback_data=f"train_structure:{course}"))
    kb.add(InlineKeyboardButton("⬅️ В главное меню", callback_data="back_main"))
    return kb


def modules_keyboard(course: str):
    kb = InlineKeyboardMarkup()
    course_dict = COURSE_CRYPTO if course == "crypto" else COURSE_TRAFFIC
    for key, (title, lessons) in course_dict.items():
        kb.add(InlineKeyboardButton(title, callback_data=f"module:{course}:{key}:0"))
    kb.add(InlineKeyboardButton("⬅️ В меню обучения", callback_data=f"back_training:{course}"))
    return kb


def lesson_nav_keyboard(course: str, module_key: str, index: int, last: bool):
    course_dict = COURSE_CRYPTO if course == "crypto" else COURSE_TRAFFIC
    keys = list(course_dict.keys())
    current_pos = keys.index(module_key)
    has_next_module = current_pos < len(keys) - 1

    kb = InlineKeyboardMarkup()
    if index > 0:
        kb.insert(
            InlineKeyboardButton(
                "⬅️ Назад",
                callback_data=f"lesson:{course}:{module_key}:{index - 1}",
            )
        )
    if not last:
        kb.insert(
            InlineKeyboardButton(
                "Дальше ▶️",
                callback_data=f"lesson:{course}:{module_key}:{index + 1}",
            )
        )
    elif has_next_module:
        kb.insert(
            InlineKeyboardButton(
                "Следующий модуль ▶️",
                callback_data=f"next_module:{course}:{module_key}",
            )
        )

    kb.add(InlineKeyboardButton("🏁 Меню обучения", callback_data=f"back_training:{course}"))
    return kb


def pay_keyboard(purchase_id: int):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("✅ Я оплатил", callback_data=f"paid:{purchase_id}"))
    kb.add(InlineKeyboardButton("⬅️ В главное меню", callback_data="back_main"))
    return kb


def back_main_inline():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("⬅️ В главное меню", callback_data="back_main"))
    return kb


# ---------------------------------------------------------------------------
# ОБУЧЕНИЕ
# ---------------------------------------------------------------------------

@dp.message_handler(lambda m: m.text == "📚 Обучение по крипте")
async def training_crypto_menu(message: types.Message):
    if is_spam(message.from_user.id):
        return
    await message.answer(
        "📚 <b>Обучение по крипте</b>\n\n"
        "База по рынку, психологии, риску и торговой системе.\n\n"
        "Выбери действие:",
        reply_markup=training_menu_keyboard("crypto"),
    )


@dp.message_handler(lambda m: m.text == "🚀 Обучение по переливу трафика")
async def training_traffic_menu(message: types.Message):
    if is_spam(message.from_user.id):
        return
    await message.answer(
        "🚀 <b>Обучение по переливу трафика</b>\n\n"
        "Показывает, как вести TikTok и приводить людей в бота.\n\n"
        "Выбери действие:",
        reply_markup=training_menu_keyboard("traffic"),
    )


@dp.callback_query_handler(lambda c: c.data.startswith("back_training:"))
async def cb_back_training(call: CallbackQuery):
    _, course = call.data.split(":")
    if course == "crypto":
        text = "📚 <b>Обучение по крипте</b>\n\nВыбери действие:"
    else:
        text = "🚀 <b>Обучение по переливу трафика</b>\n\nВыбери действие:"
    await call.message.answer(text, reply_markup=training_menu_keyboard(course))
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("train_structure:"))
async def cb_train_structure(call: CallbackQuery):
    _, course = call.data.split(":")
    if course == "crypto":
        course_dict = COURSE_CRYPTO
        title = "📚 <b>Структура курса по крипте:</b>\n"
    else:
        course_dict = COURSE_TRAFFIC
        title = "📚 <b>Структура курса по переливу:</b>\n"

    lines = [title]
    for key, (mod_title, lessons) in course_dict.items():
        lines.append(f"• {mod_title} — {len(lessons)} урок(ов)")
    lines.append("\nВыбери модуль ниже или нажми «Продолжить обучение» в меню.")

    await call.message.answer("\n".join(lines), reply_markup=modules_keyboard(course))
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("train_start:"))
async def cb_train_start(call: CallbackQuery):
    _, course = call.data.split(":")
    user_id = call.from_user.id

    course_dict = COURSE_CRYPTO if course == "crypto" else COURSE_TRAFFIC
    keys = list(course_dict.keys())

    module_key, lesson_index = get_progress(user_id, course)

    # если прогресса ещё нет — первый модуль
    if not module_key or module_key not in course_dict:
        module_key = keys[0]
        lesson_index = 0

    await send_lesson(call.message, user_id, course, module_key, lesson_index)
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("module:"))
async def cb_module(call: CallbackQuery):
    _, course, module_key, idx = call.data.split(":")
    await send_lesson(call.message, call.from_user.id, course, module_key, int(idx))
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("lesson:"))
async def cb_lesson(call: CallbackQuery):
    _, course, module_key, idx = call.data.split(":")
    await send_lesson(call.message, call.from_user.id, course, module_key, int(idx))
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("next_module:"))
async def cb_next_module(call: CallbackQuery):
    _, course, module_key = call.data.split(":")

    course_dict = COURSE_CRYPTO if course == "crypto" else COURSE_TRAFFIC
    keys = list(course_dict.keys())
    pos = keys.index(module_key)

    if pos < len(keys) - 1:
        next_key = keys[pos + 1]
        await send_lesson(call.message, call.from_user.id, course, next_key, 0)

    await call.answer()


async def send_lesson(message: types.Message, user_id: int, course: str, module_key: str, index: int):
    course_dict = COURSE_CRYPTO if course == "crypto" else COURSE_TRAFFIC
    if module_key not in course_dict:
        return

    title, lessons = course_dict[module_key]
    index = max(0, min(index, len(lessons) - 1))
    last = index == len(lessons) - 1

    header = f"🎓 <b>{title}</b>\nУрок {index + 1} из {len(lessons)}\n\n"
    text = header + lessons[index]
    kb = lesson_nav_keyboard(course, module_key, index, last)

    set_progress(user_id, course, module_key, index)
    await message.answer(text, reply_markup=kb)


# ---------------------------------------------------------------------------
# /START И ГЛАВНОЕ МЕНЮ
# ---------------------------------------------------------------------------

@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    if is_spam(message.from_user.id):
        return

    # реферал
    args = message.get_args()
    referrer_id = None
    if args.startswith("ref_"):
        try:
            referrer_tg_id = int(args.split("_", 1)[1])
            if referrer_tg_id != message.from_user.id:
                conn = db_connect()
                cur = conn.cursor()
                cur.execute("SELECT id FROM users WHERE user_id = ?", (referrer_tg_id,))
                row = cur.fetchone()
                conn.close()
                if row:
                    referrer_id = row[0]
        except Exception:
            pass

    user_db_id = get_or_create_user(message, referrer_id)
    user_row = get_user_by_tg(message.from_user.id)

    has_package_flag = has_paid_package(user_db_id)

    me = await bot.get_me()
    ref_link = f"https://t.me/{me.username}?start=ref_{message.from_user.id}"

    if has_package_flag:
        text = (
            "👋 <b>Добро пожаловать в TradeX Partner Bot!</b>\n\n"
            "Ты уже в системе: доступ к обучению и сигналам открыт, партнёрка активна.\n\n"
            "Твоя личная реферальная ссылка:\n"
            f"<code>{ref_link}</code>\n\n"
            "Выбирай нужный раздел в меню ниже 👇"
        )
    else:
        text = (
            "👋 <b>Добро пожаловать в TradeX Partner Bot!</b>\n\n"
            "Здесь ты получишь:\n"
            "• Обучение по крипте с нуля до уверенного понимания рынка.\n"
            "• Обучение по переливу трафика из TikTok в Telegram.\n"
            "• Закрытый канал с торговыми сигналами.\n"
            "• Партнёрку с выплатами <b>50%</b> с личных продаж и <b>10%</b> со второго уровня.\n\n"
            "Чтобы открыть полный доступ, канал с сигналами и партнёрку — оформи пакет за <b>100$</b>.\n"
            "После оплаты у тебя появится личная реферальная ссылка и возможность отбить вложение.\n\n"
            "Выбирай нужный раздел в меню 👇"
        )

    await message.answer(text, reply_markup=main_menu())


# ---------------------------------------------------------------------------
# ПРОДУКТ И ОПЛАТА
# ---------------------------------------------------------------------------

@dp.message_handler(lambda m: m.text == "💼 Комбо: обучение + сигналы")
async def combo_product(message: types.Message):
    if is_spam(message.from_user.id):
        return

    user_row = get_user_by_tg(message.from_user.id)
    if not user_row:
        user_db_id = get_or_create_user(message)
        user_row = get_user_by_tg(message.from_user.id)
    user_db_id = user_row[0]

    purchase_id = create_purchase(user_db_id, "package", PRICE_PACKAGE)
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT amount FROM purchases WHERE id = ?", (purchase_id,))
    amount = cur.fetchone()[0]
    conn.close()

    description = (
        "💼 <b>Комбо-продукт: обучение + сигналы + партнёрка</b>\n\n"
        "Что ты получаешь за один платёж:\n"
        "• Полный доступ к обучению по крипте внутри бота.\n"
        "• Обучение по переливу трафика из TikTok в Telegram.\n"
        "• Доступ в закрытый канал с сигналами на <b>1 месяц</b>.\n"
        "• Активация партнёрки: <b>50%</b> с личных продаж и <b>10%</b> со второго уровня.\n\n"
        "Дальше канал с сигналами можно продлевать за <b>50$</b> в месяц (без реф.начислений).\n"
    )

    pay_text = (
        f"{description}\n"
        f"<b>Сумма к оплате (USDT TRC20):</b> <code>{amount:.3f}$</code>\n"
        f"<b>Кошелёк:</b> <code>{WALLET_ADDRESS}</code>\n\n"
        "Отправь <b>точно эту сумму</b> на указанный кошелёк.\n"
        "После оплаты нажми кнопку «Я оплатил» ниже — админ сверит транзакцию и подтвердит платеж."
    )

    await message.answer(pay_text, reply_markup=pay_keyboard(purchase_id))


@dp.message_handler(lambda m: m.text == "📈 Сигналы по торговле")
async def signals_handler(message: types.Message):
    if is_spam(message.from_user.id):
        return

    user_row = get_user_by_tg(message.from_user.id)
    if not user_row:
        await message.answer("Сначала нажми /start, чтобы зарегистрироваться.", reply_markup=main_menu())
        return

    user_db_id = user_row[0]
    has_package_flag = has_paid_package(user_db_id)
    until = get_signals_until(user_db_id)

    if not has_package_flag:
        await message.answer(
            "Чтобы попасть в закрытый канал с сигналами, нужно сначала оформить основной пакет за 100$.\n\n"
            "Нажми «💼 Комбо: обучение + сигналы» в меню, чтобы открыть доступ.",
            reply_markup=main_menu(),
        )
        return

    now = datetime.utcnow()
    if until and until > now:
        text = (
            "✅ У тебя уже есть активный доступ к сигналам.\n"
            f"Доступ действует до: <b>{until.strftime('%d.%m.%Y %H:%M')}</b> (UTC).\n\n"
            "Проверяй закреплённое сообщение в канале, там вся актуальная информация."
        )
        await message.answer(text, reply_markup=main_menu())
        # пробуем пригласить в канал (если ещё не в нём)
        try:
            invite_link = await bot.export_chat_invite_link(CHANNEL_ID)
            await message.answer("Ссылка на канал с сигналами:", reply_markup=None)
            await message.answer(invite_link)
        except Exception as e:
            logger.exception("Error exporting channel link: %s", e)
        return

    # доступа нет, предлагаем продление за 50$
    purchase_id = create_purchase(user_db_id, "renewal", PRICE_RENEWAL)
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT amount FROM purchases WHERE id = ?", (purchase_id,))
    amount = cur.fetchone()[0]
    conn.close()

    text = (
        "⏳ Срок доступа в канал с сигналами истёк.\n\n"
        "Ты можешь продлить доступ ещё на <b>1 месяц</b> за <b>50$</b>.\n\n"
        f"<b>Сумма к оплате (USDT TRC20):</b> <code>{amount:.3f}$</code>\n"
        f"<b>Кошелёк:</b> <code>{WALLET_ADDRESS}</code>\n\n"
        "Отправь <b>точно эту сумму</b> на указанный кошелёк.\n"
        "После оплаты нажми «Я оплатил» — админ подтвердит продление."
    )

    await message.answer(text, reply_markup=pay_keyboard(purchase_id))


@dp.callback_query_handler(lambda c: c.data.startswith("paid:"))
async def cb_paid(call: CallbackQuery):
    _, purchase_id_str = call.data.split(":")
    purchase_id = int(purchase_id_str)

    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT p.id, p.user_id, u.user_id, u.username, u.first_name, p.amount, p.status, p.product_code "
        "FROM purchases p JOIN users u ON p.user_id = u.id WHERE p.id = ?",
        (purchase_id,),
    )
    row = cur.fetchone()
    conn.close()

    if not row:
        await call.answer("Заявка не найдена. Напиши в поддержку.", show_alert=True)
        return

    _, user_db_id, tg_id, username, first_name, amount, status, product_code = row

    if status == "paid":
        await call.answer("Эта оплата уже подтверждена ✅", show_alert=True)
        return

    user_mention = f"<a href='tg://user?id={tg_id}'>{first_name}</a>"
    uname = f"@{username}" if username else ""

    text_for_admin = (
        "💳 <b>Новая заявка на оплату</b>\n\n"
        f"Пользователь: {user_mention} {uname}\n"
        f"Telegram ID: <code>{tg_id}</code>\n"
        f"ID пользователя в БД: <code>{user_db_id}</code>\n"
        f"Тип продукта: <b>{'Пакет 100$' if product_code == 'package' else 'Продление 50$'}</b>\n"
        f"Сумма: <b>{amount:.3f}$</b>\n"
        f"ID покупки: <code>{purchase_id}</code>\n\n"
        "Если оплата пришла – нажми кнопку ниже, и бот сам начислит всё, что нужно."
    )

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("✅ Подтвердить оплату", callback_data=f"confirm:{purchase_id}"))

    await bot.send_message(ADMIN_ID, text_for_admin, reply_markup=kb)
    await call.message.answer(
        "✅ Заявка отправлена администратору.\n\n"
        "Как только оплата будет подтверждена, бот выдаст доступ и (для пакета) начислит партнёрские.",
        reply_markup=main_menu(),
    )
    await call.answer("Заявка отправлена админу", show_alert=True)


@dp.callback_query_handler(lambda c: c.data.startswith("confirm:"))
async def cb_confirm_payment(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет прав", show_alert=True)
        return

    _, purchase_id_str = call.data.split(":")
    purchase_id = int(purchase_id_str)

    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT p.id, p.user_id, u.user_id, u.first_name, p.amount, p.status, p.product_code "
        "FROM purchases p JOIN users u ON p.user_id = u.id WHERE p.id = ?",
        (purchase_id,),
    )
    row = cur.fetchone()

    if not row:
        conn.close()
        await call.answer("Покупка не найдена", show_alert=True)
        return

    _, user_db_id, buyer_tg_id, buyer_first_name, amount, status, product_code = row

    if status == "paid":
        conn.close()
        await call.answer("Уже подтверждено ✅", show_alert=True)
        return

    mark_purchase_paid(purchase_id, tx_id="admin_manual")

    # продлеваем сигналы на месяц
    new_until = extend_signals(user_db_id, days=30)

    # партнёрка только для ПЕРВОГО ПАКЕТА (product_code == 'package')
    if product_code == "package":
        lvl1_id, lvl2_id = get_referrer_chain(user_db_id)
        lvl1_bonus = amount * LEVEL1_PERCENT
        lvl2_bonus = amount * LEVEL2_PERCENT

        if lvl1_id:
            add_balance(lvl1_id, lvl1_bonus)
        if lvl2_id:
            add_balance(lvl2_id, lvl2_bonus)

        # уведомляем рефералов
        if lvl1_id:
            cur.execute("SELECT user_id FROM users WHERE id = ?", (lvl1_id,))
            r1 = cur.fetchone()
            if r1:
                try:
                    await bot.send_message(
                        r1[0],
                            f"💰 <b>Начислено {lvl1_bonus:.2f}$</b> за личную рекомендацию.\n"
                            f"Твой партнёр {buyer_first_name} оплатил пакет на {amount:.3f}$."
                    )
                except Exception:
                    pass

        if lvl2_id:
            cur.execute("SELECT user_id FROM users WHERE id = ?", (lvl2_id,))
            r2 = cur.fetchone()
            if r2:
                try:
                    await bot.send_message(
                        r2[0],
                        f"💸 <b>Начислено {lvl2_bonus:.2f}$</b> со второго уровня.\n"
                        f"Партнёр второго уровня оплатил пакет на {amount:.3f}$."
                    )
                except Exception:
                    pass

    conn.close()

    # уведомляем покупателя
    try:
        text = (
            "✅ <b>Оплата подтверждена!</b>\n\n"
            "Доступ к обучению и каналу с сигналами открыт.\n"
            f"Текущий доступ к сигналам действует до: <b>{new_until.strftime('%d.%m.%Y %H:%M')}</b> (UTC).\n\n"
            "Все нужные разделы уже доступны из главного меню."
        )
        await bot.send_message(buyer_tg_id, text, reply_markup=main_menu())
        # пригласительная ссылка в канал
        try:
            invite_link = await bot.export_chat_invite_link(CHANNEL_ID)
            await bot.send_message(buyer_tg_id, "Ссылка на канал с сигналами:")
            await bot.send_message(buyer_tg_id, invite_link)
        except Exception as e:
            logger.exception("Error exporting channel link on confirm: %s", e)
    except Exception:
        pass

    await call.answer("Оплата подтверждена ✅", show_alert=True)
    await call.message.edit_reply_markup()


# ---------------------------------------------------------------------------
# ПАРТНЁРКА И СТАТИСТИКА
# ---------------------------------------------------------------------------

@dp.message_handler(lambda m: m.text == "👥 Партнёрская программа")
async def partners_handler(message: types.Message):
    if is_spam(message.from_user.id):
        return

    user = get_user_by_tg(message.from_user.id)
    if not user:
        get_or_create_user(message)
        user = get_user_by_tg(message.from_user.id)

    user_db_id, _, username, first_name, referrer_id, balance, total_earned = user

    if not has_paid_package(user_db_id):
        await message.answer(
            "Партнёрская программа доступна только после активации пакета за 100$.\n\n"
            "Оформи пакет через «💼 Комбо: обучение + сигналы» и возвращайся сюда.",
            reply_markup=main_menu(),
        )
        return

    me = await bot.get_me()
    ref_link = f"https://t.me/{me.username}?start=ref_{message.from_user.id}"

    text = (
        "👥 <b>Партнёрская программа TradeX</b>\n\n"
        "Ты можешь зарабатывать на рекомендациях нашего продукта:\n"
        "• <b>50%</b> с каждой продажи по твоей ссылке.\n"
        "• <b>10%</b> с продаж партнёров второго уровня.\n\n"
        "Пример:\n"
        "Ты привёл друга – он купил пакет за 100$ → ты получил 50$.\n"
        "Друг привёл ещё человека → он получил 50$, а ты +10$ сверху.\n\n"
        "Твоя личная реферальная ссылка:\n"
        f"<code>{ref_link}</code>\n\n"
        f"Текущий баланс к выводу: <b>{balance:.2f}$</b>\n"
        f"Всего заработано: <b>{total_earned:.2f}$</b>\n\n"
        "Вывод средств делается через администратора. Напиши в поддержку, когда захочешь вывести прибыль."
    )

    await message.answer(text, reply_markup=main_menu())


@dp.message_handler(lambda m: m.text == "📊 Моя статистика")
async def my_stats(message: types.Message):
    if is_spam(message.from_user.id):
        return

    user = get_user_by_tg(message.from_user.id)
    if not user:
        await message.answer("Пока нет данных. Нажми /start, чтобы зарегистрироваться.", reply_markup=main_menu())
        return

    user_db_id = user[0]

    conn = db_connect()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (user_db_id,))
    lvl1_count = cur.fetchone()[0]

    cur.execute(
        "SELECT COUNT(*) FROM users WHERE referrer_id IN (SELECT id FROM users WHERE referrer_id = ?)",
        (user_db_id,),
    )
    lvl2_count = cur.fetchone()[0]

    conn.close()

    _, _, username, first_name, _, balance, total_earned = user

    text = (
        "📊 <b>Твоя статистика</b>\n\n"
        f"Имя: <b>{first_name}</b>\n"
        f"Логин: @{username if username else '—'}\n\n"
        f"Партнёров 1 уровня: <b>{lvl1_count}</b>\n"
        f"Партнёров 2 уровня: <b>{lvl2_count}</b>\n\n"
        f"Баланс к выводу: <b>{balance:.2f}$</b>\n"
        f"Всего заработано: <b>{total_earned:.2f}$</b>\n\n"
        "Продолжай делиться ссылкой и зарабатывай больше 💸"
    )

    await message.answer(text, reply_markup=main_menu())


# ---------------------------------------------------------------------------
# ПОДДЕРЖКА
# ---------------------------------------------------------------------------

@dp.message_handler(lambda m: m.text == "📩 Поддержка")
async def support_handler(message: types.Message):
    if is_spam(message.from_user.id):
        return
    await message.answer(
        f"Если возникли вопросы по оплате или работе бота – пиши в поддержку: {SUPPORT_CONTACT}",
        reply_markup=main_menu(),
    )


# ---------------------------------------------------------------------------
# ПРОЧЕЕ
# ---------------------------------------------------------------------------

@dp.callback_query_handler(lambda c: c.data == "back_main")
async def cb_back_main(call: CallbackQuery):
    await call.message.answer("Главное меню обновлено 👇")
    await call.message.answer("Выбери нужный раздел:", reply_markup=main_menu())
    await call.answer()


@dp.message_handler(commands=["admin"])
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    users_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM purchases WHERE product_code='package' AND status='paid'")
    paid_packages = cur.fetchone()[0]

    cur.execute("SELECT SUM(amount) FROM purchases WHERE status='paid'")
    total_turnover = cur.fetchone()[0] or 0.0

    conn.close()

    text = (
        "🛠 <b>Админ-панель</b>\n\n"
        f"Всего пользователей: <b>{users_count}</b>\n"
        f"Активированных пакетов (100$): <b>{paid_packages}</b>\n"
        f"Оборот по всем оплатам: <b>{total_turnover:.2f}$</b>\n\n"
        "Команды:\n"
        "/admin — эта сводка\n"
        "Все подтверждения оплат происходят через кнопки под заявками."
    )

    await message.answer(text, reply_markup=main_menu())


@dp.message_handler()
async def fallback(message: types.Message):
    if is_spam(message.from_user.id):
        return
    await message.answer("Не понял сообщение 🤔\nВыбери пункт в меню ниже.", reply_markup=main_menu())


# ---------------------------------------------------------------------------
# ФОН: ПРОВЕРКА ПРОСРОЧЕННЫХ СИГНАЛОВ (чисто на всякий случай)
# ---------------------------------------------------------------------------

async def signals_watcher():
    while True:
        try:
            now = datetime.utcnow()
            now_str = now.strftime("%Y-%m-%d %H:%M:%S")
            conn = db_connect()
            cur = conn.cursor()
            cur.execute(
                "SELECT sa.user_id, u.user_id "
                "FROM signals_access sa JOIN users u ON sa.user_id = u.id "
                "WHERE sa.active_until IS NOT NULL AND sa.active_until < ?",
                (now_str,),
            )
            rows = cur.fetchall()
            conn.close()

            for user_db_id, tg_id in rows:
                # теоретически можем кикать из канала, если бот админ
                try:
                    await bot.kick_chat_member(CHANNEL_ID, tg_id)
                    await bot.unban_chat_member(CHANNEL_ID, tg_id)
                except Exception:
                    pass
        except Exception as e:
            logger.exception("signals_watcher error: %s", e)

        await asyncio.sleep(3600)  # проверка раз в час


async def on_startup(dp: Dispatcher):
    asyncio.create_task(signals_watcher())


# ---------------------------------------------------------------------------
# ЗАПУСК
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    executor.start_polling(
        dp,
        skip_updates=True,
        on_startup=on_startup,
        allowed_updates=["message", "callback_query"],
    )
