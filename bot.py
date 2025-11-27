import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN

import aiohttp
from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.contrib.middlewares.logging import LoggingMiddleware

# ---------------------------------------------------------------------------
# НАСТРОЙКИ
# ---------------------------------------------------------------------------

BOT_TOKEN = "8330326273:AAEuWSwkqi7ypz1LZL4LXRr2jSMpKjGc36k"
ADMIN_ID = 682938643
SUPPORT_CONTACT = "@support"  # можешь заменить на свой логин

TRONGRID_API_KEY = "b33b8d65-10c9-47fb-99e0-ab47f3bbbb60"
WALLET_ADDRESS = "TSY9xF24bQ3Kbd1N1pj2w4pEEoqJow1nfpr"
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"  # USDT TRC20

CHANNEL_ID = -1003464806734  # канал с сигналами

PACKAGE_PRICE = Decimal("100.0")
RENEW_PRICE = Decimal("50.0")

LEVEL1_PERCENT = Decimal("0.5")   # 50% первому уровню
LEVEL2_PERCENT = Decimal("0.1")   # 10% второму уровню

DB_PATH = "database.db"

CHECK_PAYMENTS_INTERVAL = 60   # секунд между проверками Tron
CHECK_SUBSCRIPTIONS_INTERVAL = 300  # секунд между проверками подписок

ANTISPAM_SECONDS = 1.2  # минимальный интервал между действиями

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
            has_package INTEGER DEFAULT 0,
            signal_until TEXT,
            reg_date TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            type TEXT, -- package / renew
            base_amount REAL,
            unique_amount REAL,
            status TEXT,
            created_at TEXT,
            paid_at TEXT,
            tx_id TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE,
            module_key TEXT,
            lesson_index INTEGER
        )
        """
    )
        # новая таблица прогресса по курсам (crypto / traffic)
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


    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# УТИЛИТЫ
# ---------------------------------------------------------------------------

def decimal_str(value: Decimal) -> str:
    """Всегда 3 знака после запятой."""
    return str(value.quantize(Decimal("0.001"), rounding=ROUND_DOWN))


def now_utc_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def parse_dt(s: str):
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# ПОЛЬЗОВАТЕЛИ / РЕФЕРАЛЫ
# ---------------------------------------------------------------------------


def get_or_create_user(message: types.Message, referrer_id=None) -> int:
    user_id = message.from_user.id
    username = message.from_user.username or ""
    first_name = message.from_user.first_name or ""

    conn = db_connect()
    cur = conn.cursor()

    cur.execute("SELECT id, referrer_id FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()

    if row:
        user_db_id, _existing_ref = row
        conn.close()
        return user_db_id

    reg_date = now_utc_str()
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
        "SELECT id, user_id, username, first_name, referrer_id, balance, total_earned, has_package, signal_until "
        "FROM users WHERE user_id = ?",
        (user_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def get_user_by_db_id(user_db_id: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, user_id, username, first_name, referrer_id, balance, total_earned, has_package, signal_until "
        "FROM users WHERE id = ?",
        (user_db_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def update_user_package_and_signal(user_db_id: int, months: int = 1, set_package: bool = False):
    """Обновляем has_package и продлеваем signal_until."""
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("SELECT signal_until FROM users WHERE id = ?", (user_db_id,))
    row = cur.fetchone()
    current_until = parse_dt(row[0]) if row and row[0] else None
    now = datetime.utcnow()

    if not current_until or current_until < now:
        new_until = now + timedelta(days=30 * months)
    else:
        new_until = current_until + timedelta(days=30 * months)

    if set_package:
        cur.execute(
            "UPDATE users SET has_package = 1, signal_until = ? WHERE id = ?",
            (new_until.strftime("%Y-%m-%d %H:%M:%S"), user_db_id),
        )
    else:
        cur.execute(
            "UPDATE users SET signal_until = ? WHERE id = ?",
            (new_until.strftime("%Y-%m-%d %H:%M:%S"), user_db_id),
        )

    conn.commit()
    conn.close()


def add_balance(user_db_id: int, amount: Decimal):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET balance = balance + ?, total_earned = total_earned + ? WHERE id = ?",
        (float(amount), float(amount), user_db_id),
    )
    conn.commit()
    conn.close()


def get_ref_chain(user_db_id: int):
    """Возвращает (id 1 уровня, id 2 уровня) в таблице users."""
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT referrer_id FROM users WHERE id = ?", (user_db_id,))
    row = cur.fetchone()
    if not row or row[0] is None:
        conn.close()
        return None, None
    lvl1 = row[0]
    cur.execute("SELECT referrer_id FROM users WHERE id = ?", (lvl1,))
    row2 = cur.fetchone()
    lvl2 = row2[0] if row2 and row2[0] is not None else None
    conn.close()
    return lvl1, lvl2


# ---------------------------------------------------------------------------
# ПЛАТЕЖИ
# ---------------------------------------------------------------------------


def create_payment(user_db_id: int, p_type: str, base_amount: Decimal, unique_amount: Decimal) -> int:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO payments (user_id, type, base_amount, unique_amount, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_db_id, p_type, float(base_amount), float(unique_amount), "pending", now_utc_str()),
    )
    conn.commit()
    pid = cur.lastrowid
    conn.close()
    return pid


def get_pending_payments():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, user_id, type, base_amount, unique_amount, status, created_at "
        "FROM payments WHERE status = 'pending'"
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def mark_payment_paid(payment_id: int, tx_id: str):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "UPDATE payments SET status = 'paid', paid_at = ?, tx_id = ? WHERE id = ?",
        (now_utc_str(), tx_id, payment_id),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# ПРОГРЕСС КУРСА
# ---------------------------------------------------------------------------


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



# ---------------------------------------------------------------------------
# АНТИСПАМ
# ---------------------------------------------------------------------------

user_last_action = {}  # user_id -> datetime


def is_spam(user_id: int) -> bool:
    now = datetime.utcnow()
    last = user_last_action.get(user_id)
    user_last_action[user_id] = now
    if not last:
        return False
    return (now - last) < timedelta(seconds=ANTISPAM_SECONDS)


# ---------------------------------------------------------------------------
# КОНТЕНТ КУРСА
# ---------------------------------------------------------------------------

# ================= КУРС ПО КРИПТЕ =================

COURSE_CRYPTO = {
    "c1_mindset": (
        "Модуль 1. Психология и основы крипторынка",
        [
            "💡 <b>Урок 1. Как здесь реально зарабатывают</b>\n\n"
            "Крипта — это не казино и не волшебная кнопка удвоения депозита. "
            "Здесь зарабатывают те, кто понимает, как устроен рынок, работает по системе и умеет держать себя в руках.\n\n"
            "Твоя задача — перестать «ставить» и начать <b>торговать</b>: принимать взвешенные решения, а не играть.",
            
            "💡 <b>Урок 2. Трейдер vs инвестор</b>\n\n"
            "Трейдер:\n"
            "• держит сделку от минут до дней;\n"
            "• управляет риском в каждой позиции;\n"
            "• мыслит сериями сделок.\n\n"
            "Инвестор:\n"
            "• покупает монету в долгую;\n"
            "• переносит большие просадки;\n"
            "• опирается на фундаментал.\n\n"
            "В этом курсе мы развиваем в тебе именно трейдера, а не случайного игрока.",
            
            "💡 <b>Урок 3. Как устроена биржа</b>\n\n"
            "На бирже есть ордербук, лимитные и рыночные ордера, спред и ликвидность. "
            "Чем больше ликвидность — тем легче войти и выйти без сильного проскальзывания.\n\n"
            "Твоя задача — торговать там, где есть объёмы и деньги, а не в мёртвых монетах.",
            
            "💡 <b>Урок 4. Психологические ловушки</b>\n\n"
            "Главные враги трейдера:\n"
            "• FOMO — страх упустить движение;\n"
            "• жадность — «ещё немного подержу»;\n"
            "• желание отыграться после минуса;\n"
            "• эго — «рынок обязан развернуться».\n\n"
            "Мы будем строить систему так, чтобы эти эмоции не убивали депозит.",
        ],
    ),

    "c2_risk": (
        "Модуль 2. Риск-менеджмент и управление депозитом",
        [
            "📊 <b>Урок 1. Риск на сделку</b>\n\n"
            "Базовое правило: риск 1–2% от депозита на одну сделку.\n\n"
            "Депозит 1000$ → 1% = 10$. Это максимум, который ты можешь потерять в одной сделке. "
            "Если ты рискуешь 10–20% — это не трейдинг, а лотерея.",
            
            "📊 <b>Урок 2. Как считать объём позиции</b>\n\n"
            "Алгоритм:\n"
            "1) Определи вход и стоп-лосс.\n"
            "2) Посчитай размер стопа в %.\n"
            "3) Реши, сколько % депозита ты готов рискнуть (например, 1%).\n"
            "4) Риск в $ / стоп в % = объём позиции.\n\n"
            "Пример: депозит 500$, риск 1% (5$), стоп 4% → 5 / 0.04 = 125$ объём позиции.",
            
            "📊 <b>Урок 3. Соотношение риск / прибыль</b>\n\n"
            "Каждая сделка должна иметь R:R не хуже 1:2.\n"
            "Рискуешь 10$, потенциальная прибыль минимум 20$.\n\n"
            "Тогда даже при 40–50% прибыльных сделок ты будешь в плюсе на дистанции.",
            
            "📊 <b>Урок 4. Серии сделок и просадки</b>\n\n"
            "Слив происходит не из-за одной сделки, а из-за серии решений. "
            "Нормально иметь серию стопов. Ненормально — после серии увеличивать риск, чтобы «отбиться».\n\n"
            "Твоя цель — переживать плохие участки рынка без потери адекватного депозита.",
        ],
    ),

    "c3_tech": (
        "Модуль 3. Технический анализ без лишней воды",
        [
            "📈 <b>Урок 1. Тренд и флэт</b>\n\n"
            "Бычий тренд — последовательность более высоких максимумов и минимумов.\n"
            "Медвежий тренд — наоборот.\n"
            "Флэт — когда цена ходит в диапазоне.\n\n"
            "Мы не торгуем всё подряд. Сначала понимаем, где тренд, а где нет.",
            
            "📈 <b>Урок 2. Уровни и зоны интереса</b>\n\n"
            "Уровни — места, где цена уже реагировала: разворачивалась или задерживалась.\n"
            "Чем больше касаний, тем уровень сильнее.\n\n"
            "Мы используем уровни как ориентиры для входа, стопа и целей.",
            
            "📈 <b>Урок 3. Таймфреймы</b>\n\n"
            "Старший ТФ показывает общую картину, младший — точку входа.\n"
            "Пример: тренд смотрим на 4H, вход ищем на 15m.\n\n"
            "Не имеет смысла ловить разворот на минутках, когда на дневке сильный тренд против тебя.",
        ],
    ),

    "c4_system": (
        "Модуль 4. Торговая система и журнал",
        [
            "🧩 <b>Урок 1. Из чего состоит стратегия</b>\n\n"
            "В любой рабочей системе есть:\n"
            "• чёткие условия входа;\n"
            "• место стопа;\n"
            "• правила выхода в плюс;\n"
            "• размер риска;\n"
            "• время, когда ты торгуешь.\n\n"
            "Если что-то из этого отсутствует — это уже не система.",
            
            "🧩 <b>Урок 2. Базовый сетап по тренду</b>\n\n"
            "1) Определяем тренд на старшем ТФ.\n"
            "2) Ждём откат к зоне интереса.\n"
            "3) На младшем ТФ ждём подтверждение входа.\n"
            "4) Ставим стоп за уровень, цель — ближайшая сильная зона.\n\n"
            "Одна простая схема, которую можно спокойно повторять много раз.",
            
            "🧩 <b>Урок 3. Журнал сделок</b>\n\n"
            "Фиксируй каждую сделку: вход, стоп, цель, риск, результат и комментарий.\n"
            "Раз в неделю смотри журнал и отмечай повторяющиеся ошибки.\n\n"
            "Без журнала ты крутишься по кругу. С журналом — видишь, что реально работает.",
        ],
    ),
}

# ================= КУРС ПО ПЕРЕЛИВУ ТРАФИКА =================

COURSE_TRAFFIC = {
    "t1_profile": (
        "Модуль 1. Позиционирование и профиль",
        [
            "🚀 <b>Урок 1. Зачем тебе TikTok</b>\n\n"
            "TikTok — это витрина. Основная цель: привести людей в Telegram-бота, "
            "где они получают обучение, сигналы и партнёрку.\n\n"
            "Каждый ролик — это приглашение в твою экосистему, а не просто развлечение.",
            
            "🚀 <b>Урок 2. Оформление профиля</b>\n\n"
            "Профиль должен за пару секунд объяснять, чем ты полезен:\n"
            "• аватар, связанный с деньгами/криптой;\n"
            "• понятное описание, кто ты;\n"
            "• призыв: «Обучение и закрытый канал — ссылка в профиле».\n\n"
            "Без нормального профиля даже хорошие ролики сливают трафик.",
            
            "🚀 <b>Урок 3. Триггеры доверия</b>\n\n"
            "Люди охотнее переходят в бот, когда видят:\n"
            "• честные истории и твой путь;\n"
            "• разбор ошибок новичков;\n"
            "• адекватное отношение к рискам, без сказок про «миллион за месяц».\n\n"
            "Добавляй такие элементы в контент — это сильно поднимает конверсию.",
        ],
    ),

    "t2_content": (
        "Модуль 2. Контент, который приводит людей",
        [
            "🎥 <b>Урок 1. Структура ролика</b>\n\n"
            "Рабочая схема:\n"
            "1) Крючок в первые секунды (боль, вопрос, сильная фраза);\n"
            "2) Короткое раскрытие мысли;\n"
            "3) пример/история;\n"
            "4) призыв перейти в бота по ссылке в профиле.\n\n"
            "Без призыва люди просто смотрят и листают дальше.",
            
            "🎥 <b>Урок 2. Темы, которые заходят лучше всего</b>\n\n"
            "• ошибки новичков в крипте;\n"
            "• реальные кейсы заработка / слива;\n"
            "• объяснение, чем трейдинг отличается от казино;\n"
            "• как можно отбить свои 100$ через партнёрку.\n\n"
            "Каждый ролик должен логично подводить к боту.",
            
            "🎥 <b>Урок 3. Регулярность и план</b>\n\n"
            "Один ролик в день стабильно лучше, чем 10 роликов раз в неделю.\n"
            "Составь список тем на неделю вперёд и снимай партиями.\n\n"
            "Системный контент = системный трафик.",
        ],
    ),

    "t3_funnel": (
        "Модуль 3. Воронка: от ролика до оплаты",
        [
            "📲 <b>Урок 1. Путь пользователя</b>\n\n"
            "Классический путь:\n"
            "TikTok → профиль → ссылка → бот → /start → обучение и оффер на 100$.\n\n"
            "Наша задача — сделать этот путь логичным и понятным даже для новичка.",
            
            "📲 <b>Урок 2. Как объяснять продукт за 100$</b>\n\n"
            "Человек должен чётко понимать, за что он платит:\n"
            "• обучение по крипте внутри бота;\n"
            "• доступ к сигналам;\n"
            "• партнёрка 50%/10%.\n\n"
            "Плюс — честно говорим, что можно отбить вложение, приведя всего нескольких людей.",
            
            "📲 <b>Урок 3. Зачем партнёрка и на чём ты зарабатываешь</b>\n\n"
            "Ты не просто продаёшь курс. Ты строишь систему, где:\n"
            "• люди получают реальный продукт;\n"
            "• могут зарабатывать по партнёрке;\n"
            "• ты зарабатываешь вместе с ними.\n\n"
            "Важно — без пирамид и абсурдных обещаний. Чистая математика и прозрачные условия.",
        ],
    ),
}




# ---------------------------------------------------------------------------
# КЛАВИАТУРЫ
# ---------------------------------------------------------------------------


def main_menu(has_package: bool):
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton("📚 Обучение по крипте"))
    kb.row(KeyboardButton("🚀 Обучение по переливу трафика"))
    kb.row(KeyboardButton("📩 Поддержка"))
    if has_package:
        kb.row(KeyboardButton("📈 Сигналы по торговле"))
        kb.row(KeyboardButton("👥 Партнёрская программа"), KeyboardButton("📊 Моя статистика"))
        kb.row(KeyboardButton("🏆 Топ партнёров"))
    else:
        kb.row(KeyboardButton("🔥 Что я получу за 100$"))
    return kb


def training_menu_keyboard():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("▶️ Продолжить обучение", callback_data="train_start"))
    kb.add(InlineKeyboardButton("📚 Структура курса", callback_data="train_structure"))
    kb.add(InlineKeyboardButton("⬅️ В главное меню", callback_data="back_main"))
    return kb


def modules_keyboard():
    kb = InlineKeyboardMarkup()
    for key, (title, _lessons) in COURSE.items():
        kb.add(InlineKeyboardButton(title, callback_data=f"module:{key}:0"))
    kb.add(InlineKeyboardButton("⬅️ В меню обучения", callback_data="back_training"))
    return kb



def lesson_nav_keyboard(module_key: str, index: int, last: bool):
    kb = InlineKeyboardMarkup()
    if index > 0:
        kb.insert(InlineKeyboardButton("⬅️ Назад", callback_data=f"lesson:{module_key}:{index - 1}"))
    if not last:
        kb.insert(InlineKeyboardButton("Дальше ▶️", callback_data=f"lesson:{module_key}:{index + 1}"))
    kb.add(InlineKeyboardButton("🏁 Меню обучения", callback_data="back_training"))
    return kb


def payment_keyboard(payment_id: int):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("✅ Я оплатил", callback_data=f"paid:{payment_id}"))
    kb.add(InlineKeyboardButton("⬅️ В главное меню", callback_data="back_main"))
    return kb


def renew_keyboard(payment_id: int):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("✅ Я оплатил продление", callback_data=f"paid:{payment_id}"))
    kb.add(InlineKeyboardButton("⬅️ В главное меню", callback_data="back_main"))
    return kb


def back_main_inline():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("⬅️ В главное меню", callback_data="back_main"))
    return kb


# ---------------------------------------------------------------------------
# ХЭНДЛЕРЫ
# ---------------------------------------------------------------------------

@dp.message_handler(lambda m: m.text == "📚 Обучение по крипте")
async def training_crypto_menu(message: types.Message):
    if is_spam(message.from_user.id):
        return
    await message.answer(
        "📚 <b>Обучение по крипте</b>\n\n"
        "Это базовый курс по рынку, психологии, рискам и торговой системе.\n\n"
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


def training_menu_keyboard(course: str):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("▶️ Продолжить обучение", callback_data=f"train_start:{course}"))
    kb.add(InlineKeyboardButton("📚 Структура курса", callback_data=f"train_structure:{course}"))
    kb.add(InlineKeyboardButton("⬅️ В главное меню", callback_data="back_main"))
    return kb


def modules_keyboard(course: str):
    kb = InlineKeyboardMarkup()
    if course == "crypto":
        course_dict = COURSE_CRYPTO
    else:
        course_dict = COURSE_TRAFFIC

    for key, (title, lessons) in course_dict.items():
        kb.add(InlineKeyboardButton(title, callback_data=f"module:{course}:{key}:0"))
    kb.add(InlineKeyboardButton("⬅️ В меню обучения", callback_data=f"back_training:{course}"))
    return kb


def lesson_nav_keyboard(course: str, module_key: str, index: int, last: bool):
    if course == "crypto":
        keys = list(COURSE_CRYPTO.keys())
    else:
        keys = list(COURSE_TRAFFIC.keys())

    current_pos = keys.index(module_key)
    has_next_module = (current_pos < len(keys) - 1)

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
        # последний урок модуля — даём кнопку перехода к следующему модулю
        kb.insert(
            InlineKeyboardButton(
                "Следующий модуль ▶️",
                callback_data=f"next_module:{course}:{module_key}",
            )
        )

    kb.add(InlineKeyboardButton("🏁 Меню обучения", callback_data=f"back_training:{course}"))
    return kb



@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    if is_spam(message.from_user.id):
        return

    args = ""
    try:
        args = message.get_args()
    except Exception:
        pass

    referrer_id = None
    if args:
        try:
            if args.startswith("ref_"):
                ref_tg_id = int(args.split("_", 1)[1])
                if ref_tg_id != message.from_user.id:
                    conn = db_connect()
                    cur = conn.cursor()
                    cur.execute("SELECT id FROM users WHERE user_id = ?", (ref_tg_id,))
                    row = cur.fetchone()
                    conn.close()
                    if row:
                        referrer_id = row[0]
        except Exception:
            pass

    user_db_id = get_or_create_user(message, referrer_id)
    user = get_user_by_db_id(user_db_id)
    has_package = bool(user[7])

    text = (
        "👋 <b>Добро пожаловать в систему крипто-партнёрства!</b>\n\n"
        "Здесь ты получишь:\n"
        "• Полный курс по трейдингу в крипте.\n"
        "• Обучение по переливу трафика из TikTok в Telegram.\n"
        "• Доступ к закрытому каналу с сигналами.\n"
        "• Двухуровневую реферальную систему: <b>50%</b> с личных продаж и <b>10%</b> со второго уровня.\n\n"
        "Ты покупаешь доступ один раз за <b>100$</b>, получаешь обучение и партнёрку навсегда, "
        "а первый месяц сигналов включён в эту сумму.\n\n"
        "После оплаты ты сможешь приглашать людей по своей ссылке и зарабатывать на рекомендациях.\n\n"
        "Выбирай нужный раздел в меню 👇"
    )

    await message.answer(text, reply_markup=main_menu(has_package))


@dp.message_handler(lambda m: m.text == "📩 Поддержка")
async def support_handler(message: types.Message):
    if is_spam(message.from_user.id):
        return
    await message.answer(
        f"Если возникли вопросы по оплате, подписке или работе бота — пиши в поддержку: {SUPPORT_CONTACT}",
    )


@dp.message_handler(lambda m: m.text == "🔥 Что я получу за 100$")
async def about_package(message: types.Message):
    if is_spam(message.from_user.id):
        return

    user = get_user_by_tg(message.from_user.id)
    has_package = bool(user[7]) if user else False

    if has_package:
        await message.answer("У тебя уже активирован полный доступ ✅", reply_markup=main_menu(True))
        return

    # уникальный хвост суммы по tg_id
    unique_suffix = Decimal(str((message.from_user.id % 1000) / 1000)).quantize(Decimal("0.001"))
    unique_amount = (PACKAGE_PRICE + unique_suffix).quantize(Decimal("0.001"))
    user_db_id = get_or_create_user(message)
    payment_id = create_payment(user_db_id, "package", PACKAGE_PRICE, unique_amount)

    text = (
        "🔥 <b>Полный доступ за 100$</b>\n\n"
        "Что входит в один платёж:\n"
        "• Полное обучение по крипто-трейдингу.\n"
        "• Обучение по переливу трафика из TikTok в Telegram.\n"
        "• Первый месяц доступа к каналу с сигналами.\n"
        "• Доступ к партнёрской программе 50% / 10% <b>навсегда</b>.\n\n"
        "👉 Ты можешь не просто отбить свои 100$, но и выйти в стабильный плюс, "
        "приглашая людей в систему.\n\n"
        "<b>Как оплатить:</b>\n"
        f"1. Переведи ровно <b>{decimal_str(unique_amount)} USDT (TRC20)</b> на кошелёк:\n"
        f"<code>{WALLET_ADDRESS}</code>\n"
        "2. Обязательно отправь <b>ТОЧНО ЭТУ СУММУ</b>, чтобы бот смог автоматически найти твою транзакцию.\n"
        "3. После перевода нажми кнопку «Я оплатил» ниже.\n\n"
        "Бот проверит блокчейн и выдаст доступ автоматически ✅"
    )

    await message.answer(text, reply_markup=payment_keyboard(payment_id))


@dp.message_handler(lambda m: m.text == "📚 Обучение по крипте")
async def crypto_training(message: types.Message):
    if is_spam(message.from_user.id):
        return
    await message.answer(
        "📚 <b>Обучение по крипто-трейдингу</b>\n\n"
        "Это структурированный курс от психологии до рабочей торговой системы.\n\n"
        "Выбери действие:",
        reply_markup=training_menu_keyboard(),
    )


@dp.message_handler(lambda m: m.text == "🚀 Обучение по переливу трафика")
async def traffic_training(message: types.Message):
    if is_spam(message.from_user.id):
        return
    await message.answer(
        "🚀 <b>Обучение по переливу трафика</b>\n\n"
        "Здесь ты получишь понимание, как вести людей из TikTok в этого бота и зарабатывать на рекомендациях.\n\n"
        "Выбери модуль в структуре курса или продолжи обучение:",
        reply_markup=training_menu_keyboard(),
    )


# -------------------- ОБУЧЕНИЕ -------------------- #

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

    module_key, lesson_index = get_progress(user_id, course)

    if course == "crypto":
        course_dict = COURSE_CRYPTO
        keys = list(COURSE_CRYPTO.keys())
    else:
        course_dict = COURSE_TRAFFIC
        keys = list(COURSE_TRAFFIC.keys())

    # если прогресса ещё нет — начинаем с первого модуля
    if not module_key or module_key not in course_dict:
        module_key = keys[0]
        lesson_index = 0

    await send_lesson(call.message, user_id, course, module_key, lesson_index)
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("module:"))
async def cb_module(call: CallbackQuery):
    _, course, module_key, idx = call.data.split(":")
    user_id = call.from_user.id
    await send_lesson(call.message, user_id, course, module_key, int(idx))
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("lesson:"))
async def cb_lesson(call: CallbackQuery):
    _, course, module_key, idx = call.data.split(":")
    user_id = call.from_user.id
    await send_lesson(call.message, user_id, course, module_key, int(idx))
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("next_module:"))
async def cb_next_module(call: CallbackQuery):
    _, course, module_key = call.data.split(":")

    if course == "crypto":
        keys = list(COURSE_CRYPTO.keys())
        course_dict = COURSE_CRYPTO
    else:
        keys = list(COURSE_TRAFFIC.keys())
        course_dict = COURSE_TRAFFIC

    pos = keys.index(module_key)
    if pos < len(keys) - 1:
        next_module_key = keys[pos + 1]
        user_id = call.from_user.id
        await send_lesson(call.message, user_id, course, next_module_key, 0)

    await call.answer()


async def send_lesson(message: types.Message, user_id: int, course: str, module_key: str, index: int):
    if course == "crypto":
        course_dict = COURSE_CRYPTO
    else:
        course_dict = COURSE_TRAFFIC

    if module_key not in course_dict:
        return

    title, lessons = course_dict[module_key]
    index = max(0, min(index, len(lessons) - 1))
    last = (index == len(lessons) - 1)

    header = f"🎓 <b>{title}</b>\nУрок {index + 1} из {len(lessons)}\n\n"
    text = header + lessons[index]
    kb = lesson_nav_keyboard(course, module_key, index, last)

    set_progress(user_id, course, module_key, index)
    await message.answer(text, reply_markup=kb)





@dp.callback_query_handler(lambda c: c.data == "back_main")
async def cb_back_main(call: types.CallbackQuery):
    user = get_user_by_tg(call.from_user.id)
    has_package = bool(user[7]) if user else False

    try:
        await call.message.answer("Главное меню обновлено 👇")
        await call.message.answer("Выбери нужный раздел:", reply_markup=main_menu(has_package))
    except Exception as e:
        logging.exception("back_main error: %s", e)

    await call.answer()



# --------------------- СИГНАЛЫ И ПРОДЛЕНИЕ ---------------------

@dp.message_handler(lambda m: m.text == "📈 Сигналы по торговле")
async def signals_handler(message: types.Message):
    if is_spam(message.from_user.id):
        return

    user = get_user_by_tg(message.from_user.id)
    if not user:
        await message.answer("Нажми /start, чтобы зарегистрироваться.")
        return

    has_package = bool(user[7])
    signal_until_str = user[8]
    signal_until = parse_dt(signal_until_str) if signal_until_str else None
    now = datetime.utcnow()

    if not has_package:
        await message.answer(
            "Чтобы получить доступ к сигналам, сначала активируй полный пакет за 100$.",
            reply_markup=main_menu(False),
        )
        return

    if signal_until and signal_until > now:
        # доступ есть → даём инвайт
        try:
            invite_link = await bot.export_chat_invite_link(CHANNEL_ID)
            await message.answer(
                "📈 <b>Твой доступ к сигналам активен.</b>\n\n"
                "Вот ссылка на закрытый канал с сигналами:\n"
                f"{invite_link}\n\n"
                "Если ссылка не работает — напиши в поддержку.",
            )
        except Exception as e:
            logger.exception("Failed to export invite link: %s", e)
            await message.answer(
                f"Не получилось получить ссылку на канал. Напиши в поддержку: {SUPPORT_CONTACT}"
            )
        return

    # нужно продление
    unique_suffix = Decimal(str((message.from_user.id % 1000) / 1000)).quantize(Decimal("0.001"))
    unique_amount = (RENEW_PRICE + unique_suffix).quantize(Decimal("0.001"))
    user_db_id = get_or_create_user(message)
    payment_id = create_payment(user_db_id, "renew", RENEW_PRICE, unique_amount)

    text = (
        "⏳ <b>Подписка на сигнальный канал закончилась.</b>\n\n"
        "Продление стоит <b>50$</b> за 30 дней.\n\n"
        "Важно: с продления сигнального канала реферальные бонусы не начисляются — "
        "весь этот платёж идёт на поддержку работы канала.\n\n"
        "<b>Как оплатить продление:</b>\n"
        f"1. Переведи ровно <b>{decimal_str(unique_amount)} USDT (TRC20)</b> на кошелёк:\n"
        f"<code>{WALLET_ADDRESS}</code>\n"
        "2. Обязательно отправь <b>ТОЧНО ЭТУ СУММУ</b>.\n"
        "3. После перевода нажми кнопку «Я оплатил продление» ниже.\n\n"
        "Бот проверит блокчейн и автоматически продлит доступ ✅"
    )

    await message.answer(text, reply_markup=renew_keyboard(payment_id))


# --------------------- ПАРТНЁРКА И СТАТИСТИКА ---------------------


@dp.message_handler(lambda m: m.text == "👥 Партнёрская программа")
async def partners_handler(message: types.Message):
    if is_spam(message.from_user.id):
        return

    user = get_user_by_tg(message.from_user.id)
    if not user:
        get_or_create_user(message)
        user = get_user_by_tg(message.from_user.id)

    has_package = bool(user[7])
    if not has_package:
        await message.answer(
            "Партнёрская программа доступна только после покупки полного пакета за 100$.",
            reply_markup=main_menu(False),
        )
        return

    _user_db_id, _, username, first_name, _, balance, total_earned, *_ = user
    me = await bot.get_me()
    ref_link = f"https://t.me/{me.username}?start=ref_{message.from_user.id}"

    text = (
        "👥 <b>Партнёрская программа</b>\n\n"
        "Ты можешь зарабатывать на рекомендациях этого бота:\n"
        "• <b>50%</b> с каждой продажи по твоей ссылке.\n"
        "• <b>10%</b> с продаж партнёров второго уровня.\n\n"
        "Пример:\n"
        "— Ты привёл человека, он купил пакет за 100$ → ты получил 50$.\n"
        "— Он привёл ещё человека → он получил 50$, а ты +10$ вторым уровнем.\n\n"
        "Твоя личная ссылка:\n"
        f"<code>{ref_link}</code>\n\n"
        f"Текущий баланс к выводу: <b>{balance:.2f}$</b>\n"
        f"Всего заработано: <b>{total_earned:.2f}$</b>\n\n"
        "Ты не можешь уйти в минус: максимум, что ты теряешь — свои первые 100$, "
        "которые быстро отбиваются даже при небольшом количестве продаж."
    )

    await message.answer(text)


@dp.message_handler(lambda m: m.text == "📊 Моя статистика")
async def my_stats(message: types.Message):
    if is_spam(message.from_user.id):
        return

    user = get_user_by_tg(message.from_user.id)
    if not user:
        await message.answer("Пока нет данных. Нажми /start, чтобы зарегистрироваться.")
        return

    user_db_id, _, username, first_name, _, balance, total_earned, *_ = user

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

    text = (
        "📊 <b>Твоя статистика</b>\n\n"
        f"Имя: <b>{first_name}</b>\n"
        f"Баланс к выводу: <b>{balance:.2f}$</b>\n"
        f"Всего заработано: <b>{total_earned:.2f}$</b>\n\n"
        f"Партнёров 1 уровня: <b>{lvl1_count}</b>\n"
        f"Партнёров 2 уровня: <b>{lvl2_count}</b>\n\n"
        "Продолжай делиться своей ссылкой и усиливать трафик 🚀"
    )

    await message.answer(text)


@dp.message_handler(lambda m: m.text == "🏆 Топ партнёров")
async def top_partners(message: types.Message):
    if is_spam(message.from_user.id):
        return

    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT first_name, total_earned FROM users WHERE total_earned > 0 ORDER BY total_earned DESC LIMIT 10"
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await message.answer("Пока ещё никто не заработал по партнёрской программе. Всё впереди 💪")
        return

    lines = ["🏆 <b>Топ партнёров по заработку</b>\n"]
    for idx, (first_name, total_earned) in enumerate(rows, start=1):
        name = first_name or "Без имени"
        lines.append(f"{idx}. {name} — <b>{total_earned:.2f}$</b>")

    lines.append("\nНикто не видит ссылки и контакты других партнёров — только результаты.")
    await message.answer("\n".join(lines))


# --------------------- ОПЛАТА: КНОПКА "Я ОПЛАТИЛ" ---------------------


@dp.callback_query_handler(lambda c: c.data.startswith("paid:"))
async def cb_paid(call: types.CallbackQuery):
    if is_spam(call.from_user.id):
        await call.answer()
        return

    _, pid_str = call.data.split(":")
    payment_id = int(pid_str)

    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, user_id, type, base_amount, unique_amount, status FROM payments WHERE id = ?",
        (payment_id,),
    )
    row = cur.fetchone()
    conn.close()

    if not row:
        await call.answer("Платёж не найден. Напиши в поддержку.", show_alert=True)
        return

    _pid, user_db_id, p_type, base_amount, unique_amount, status = row

    if status == "paid":
        await call.answer("Этот платёж уже подтверждён ✅", show_alert=True)
        return

    await call.message.answer(
        "✅ Заявка на проверку оплаты зафиксирована.\n\n"
        "Бот периодически проверяет блокчейн. Как только транзакция будет найдена, доступ будет выдан автоматически.",
    )
    await call.answer("Ожидаем подтверждения транзакции в сети.", show_alert=True)


# --------------------- АДМИН-КОМАНДЫ ---------------------


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


@dp.message_handler(commands=["admin"])
async def admin_panel(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    total_users = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM users WHERE has_package = 1")
    buyers = cur.fetchone()[0]
    conn.close()

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🏆 Топ партнёров (админ)", callback_data="admin_top"))

    text = (
        "👨‍💻 <b>Админ-панель</b>\n\n"
        f"Всего пользователей: <b>{total_users}</b>\n"
        f"Купили полный доступ: <b>{buyers}</b>\n\n"
        "Команды:\n"
        "• <code>/user &lt;tg_id&gt;</code> — инфо по пользователю\n"
        "• <code>/give_package &lt;tg_id&gt;</code> — выдать пакет 100$\n"
        "• <code>/give_signals &lt;tg_id&gt;</code> — выдать +30 дней сигналов\n\n"
        "Выбери действие:"
    )

    await message.answer(text, reply_markup=kb)


@dp.callback_query_handler(lambda c: c.data == "admin_top")
async def admin_top(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer()
        return

    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, user_id, first_name, total_earned FROM users WHERE total_earned > 0 "
        "ORDER BY total_earned DESC LIMIT 20"
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await call.message.answer("Пока никто не заработал по партнёрке.")
        await call.answer()
        return

    lines = ["🏆 <b>Топ партнёров (админ)</b>\n"]
    for idx, (user_db_id, tg_id, first_name, total_earned) in enumerate(rows, start=1):
        lines.append(
            f"{idx}. ID в БД: <code>{user_db_id}</code> | TG ID: <code>{tg_id}</code> | "
            f"Имя: {first_name or '—'} | Заработано: <b>{total_earned:.2f}$</b>"
        )

    await call.message.answer("\n".join(lines))
    await call.answer()


@dp.message_handler(commands=["user"])
async def admin_user_info(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: <code>/user &lt;tg_id&gt;</code>")
        return

    try:
        tg_id = int(parts[1])
    except ValueError:
        await message.answer("tg_id должен быть числом.")
        return

    user = get_user_by_tg(tg_id)
    if not user:
        await message.answer("Пользователь не найден.")
        return

    user_db_id, _tid, username, first_name, referrer_id, balance, total_earned, has_package, signal_until = user

    text = (
        f"👤 <b>Пользователь</b>\n\n"
        f"ID в БД: <code>{user_db_id}</code>\n"
        f"TG ID: <code>{tg_id}</code>\n"
        f"Имя: {first_name}\n"
        f"Username: @{username if username else '—'}\n"
        f"Реферер (id в БД): {referrer_id if referrer_id else '—'}\n"
        f"has_package: {has_package}\n"
        f"signal_until: {signal_until or '—'}\n"
        f"Баланс: <b>{balance:.2f}$</b>\n"
        f"Всего заработано: <b>{total_earned:.2f}$</b>\n"
    )

    await message.answer(text)


@dp.message_handler(commands=["give_package"])
async def admin_give_package(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: <code>/give_package &lt;tg_id&gt;</code>")
        return

    try:
        tg_id = int(parts[1])
    except ValueError:
        await message.answer("tg_id должен быть числом.")
        return

    # найдём или создадим
    fake_message = message  # переиспользуем объект
    fake_message.from_user.id = tg_id  # костыль, но ок для get_or_create
    user_db_id = get_or_create_user(fake_message)
    update_user_package_and_signal(user_db_id, months=1, set_package=True)

    await message.answer(f"Пакет выдан пользователю с TG ID {tg_id}.")

    try:
        await bot.send_message(
            tg_id,
            "🎁 <b>Тебе выдан полный доступ администратором.</b>\n\n"
            "Обучение, партнёрка и первый месяц сигналов уже доступны в меню.",
        )
    except Exception:
        pass


@dp.message_handler(commands=["give_signals"])
async def admin_give_signals(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: <code>/give_signals &lt;tg_id&gt;</code>")
        return

    try:
        tg_id = int(parts[1])
    except ValueError:
        await message.answer("tg_id должен быть числом.")
        return

    fake_message = message
    fake_message.from_user.id = tg_id
    user_db_id = get_or_create_user(fake_message)
    update_user_package_and_signal(user_db_id, months=1, set_package=False)

    await message.answer(f"Подписка на сигналы продлена пользователю с TG ID {tg_id}.")

    try:
        await bot.send_message(
            tg_id,
            "🎁 <b>Тебе продлили доступ к сигналам администратором.</b>\n\n"
            "Загляни в раздел «📈 Сигналы по торговле».",
        )
    except Exception:
        pass


# --------------------- Fallback ---------------------


@dp.message_handler()
async def fallback(message: types.Message):
    if is_spam(message.from_user.id):
        return

    user = get_user_by_tg(message.from_user.id)
    has_package = bool(user[7]) if user else False
    await message.answer(
        "Не понял сообщение 🤔\nВыбери пункт в меню ниже.",
        reply_markup=main_menu(has_package),
    )


# ---------------------------------------------------------------------------
# ФОНОВЫЙ МОНИТОРИНГ TRON
# ---------------------------------------------------------------------------


async def fetch_trc20_transactions(session: aiohttp.ClientSession):
    url = f"https://api.trongrid.io/v1/accounts/{WALLET_ADDRESS}/transactions/trc20"
    params = {
        "limit": 200,
        "only_to": "true",
        "contract_address": USDT_CONTRACT,
    }
    headers = {"TRON-PRO-API-KEY": TRONGRID_API_KEY}
    async with session.get(url, params=params, headers=headers, timeout=20) as resp:
        if resp.status != 200:
            logger.warning("TronGrid error: %s", await resp.text())
            return []
        data = await resp.json()
        return data.get("data", [])


async def payments_watcher():
    await asyncio.sleep(10)  # дать боту запуститься
    while True:
        try:
            pending = get_pending_payments()
            if pending:
                async with aiohttp.ClientSession() as session:
                    txs = await fetch_trc20_transactions(session)

                if txs:
                    tx_map = {}  # amount_str -> tx_id
                    for tx in txs:
                        try:
                            value = Decimal(tx["value"])
                            decimals = int(tx.get("token_info", {}).get("decimals", 6))
                            amount = (value / (Decimal(10) ** decimals)).quantize(Decimal("0.001"))
                            amount_str = decimal_str(amount)
                            tx_id = tx.get("transaction_id")
                            tx_map[amount_str] = tx_id
                        except Exception:
                            continue

                    for pid, user_db_id, p_type, base_amount, unique_amount, status, created_at in pending:
                        unique_dec = Decimal(str(unique_amount)).quantize(Decimal("0.001"))
                        ustr = decimal_str(unique_dec)
                        if ustr in tx_map:
                            tx_id = tx_map[ustr]
                            mark_payment_paid(pid, tx_id)

                            user = get_user_by_db_id(user_db_id)
                            if not user:
                                continue

                            if p_type == "package":
                                update_user_package_and_signal(user_db_id, months=1, set_package=True)
                                lvl1, lvl2 = get_ref_chain(user_db_id)
                                amount_dec = Decimal(str(base_amount))
                                lvl1_bonus = (amount_dec * LEVEL1_PERCENT).quantize(Decimal("0.01"))
                                lvl2_bonus = (amount_dec * LEVEL2_PERCENT).quantize(Decimal("0.01"))

                                if lvl1:
                                    add_balance(lvl1, lvl1_bonus)
                                if lvl2:
                                    add_balance(lvl2, lvl2_bonus)

                                buyer_tg_id = user[1]
                                try:
                                    await bot.send_message(
                                        buyer_tg_id,
                                        "✅ <b>Оплата пакета за 100$ подтверждена!</b>\n\n"
                                        "Тебе открыт полный доступ к обучению, партнёрке и первому месяцу сигналов.\n"
                                        "Разделы уже доступны в главном меню.",
                                    )
                                except Exception:
                                    pass

                            elif p_type == "renew":
                                update_user_package_and_signal(user_db_id, months=1, set_package=False)
                                buyer_tg_id = user[1]
                                try:
                                    await bot.send_message(
                                        buyer_tg_id,
                                        "✅ <b>Продление сигналов подтверждено!</b>\n\n"
                                        "Твой доступ к сигнальному каналу продлён ещё на 30 дней.",
                                    )
                                except Exception:
                                    pass

            await asyncio.sleep(CHECK_PAYMENTS_INTERVAL)

        except Exception as e:
            logger.exception("Error in payments_watcher: %s", e)
            await asyncio.sleep(CHECK_PAYMENTS_INTERVAL)


async def subscriptions_watcher():
    await asyncio.sleep(15)
    while True:
        try:
            conn = db_connect()
            cur = conn.cursor()
            now_str = now_utc_str()
            cur.execute(
                "SELECT id, user_id, signal_until FROM users WHERE signal_until IS NOT NULL AND signal_until < ?",
                (now_str,),
            )
            rows = cur.fetchall()
            conn.close()

            for user_db_id, tg_id, signal_until in rows:
                try:
                    await bot.kick_chat_member(CHANNEL_ID, tg_id)
                except Exception:
                    pass

            await asyncio.sleep(CHECK_SUBSCRIPTIONS_INTERVAL)
        except Exception as e:
            logger.exception("Error in subscriptions_watcher: %s", e)
            await asyncio.sleep(CHECK_SUBSCRIPTIONS_INTERVAL)


async def on_startup(dp: Dispatcher):
    loop = asyncio.get_event_loop()
    loop.create_task(payments_watcher())
    loop.create_task(subscriptions_watcher())
    logger.info("Background tasks started")


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

