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


def set_progress(user_id: int, module_key: str, lesson_index: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT id FROM progress WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if row:
        cur.execute(
            "UPDATE progress SET module_key = ?, lesson_index = ? WHERE user_id = ?",
            (module_key, lesson_index, user_id),
        )
    else:
        cur.execute(
            "INSERT INTO progress (user_id, module_key, lesson_index) VALUES (?, ?, ?)",
            (user_id, module_key, lesson_index),
        )
    conn.commit()
    conn.close()


def get_progress(user_id: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT module_key, lesson_index FROM progress WHERE user_id = ?", (user_id,))
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

COURSE = {
    "crypto_mindset": (
        "Модуль 1. Психология и основы крипторынка",
        [
            "💡 <b>Урок 1. Как здесь реально зарабатывают</b>\n\n"
            "Крипта — это не казино и не волшебный способ удвоить депозит за ночь. "
            "Здесь зарабатывают те, кто:\n"
            "• понимает, как работает рынок;\n"
            "• принимает решения по системе, а не по эмоциям;\n"
            "• умеет держать риск под контролем.\n\n"
            "Твоя задача в этом курсе — перестать \"угадывать\" и начать мыслить как трейдер: "
            "в терминах вероятностей, дистанции и риск/прибыль.",

            "💡 <b>Урок 2. Кто такой трейдер и чем он отличается от инвестора</b>\n\n"
            "Трейдер:\n"
            "• заходит в рынок на ограниченное время;\n"
            "• работает по чёткой стратегии входа/выхода;\n"
            "• мыслит серией сделок, а не одной ставкой.\n\n"
            "Инвестор:\n"
            "• покупает активы \"в долгую\";\n"
            "• переносит просадку месяцами;\n"
            "• смотрит на фундаментал.\n\n"
            "В этом курсе фокус на трейдинге: быстрых, но контролируемых сделках.",

            "💡 <b>Урок 3. Почему 90% сливают депозиты</b>\n\n"
            "Главные причины:\n"
            "• торгуют без системы — просто \"кажется, сейчас вырастет\";\n"
            "• завышают риск — заходят всем депозитом или большим плечом;\n"
            "• не признают ошибки и не режут убытки;\n"
            "• пытаются отыграться после минусовой сделки.\n\n"
            "Твоя цель — попасть в те самые 10%, кто соблюдает правила и зарабатывает на дисциплине.",

            "💡 <b>Урок 4. Правило одной сделки</b>\n\n"
            "Простой фильтр перед входом:\n"
            "Представь, что ты можешь сделать <b>только одну сделку в жизни</b>. "
            "Зайдёшь ли ты в эту сделку по текущему сигналу?\n\n"
            "Если ответ \"нет\" — вход слабый. Это автоматически отрубает кучу импульсивных и глупых решений.",
        ],
    ),
    "crypto_risk": (
        "Модуль 2. Риск-менеджмент и размер позиции",
        [
            "📊 <b>Урок 1. Сколько можно рисковать в одной сделке</b>\n\n"
            "Золотое правило: риск на одну сделку — не более 1–2% от депозита.\n\n"
            "Если у тебя 1000$, то риск 1% — это 10$. "
            "Даже серия минусовых сделок не убьёт счёт, и ты сможешь восстановиться за счёт плюсовых входов.",

            "📊 <b>Урок 2. Как считать объём позиции</b>\n\n"
            "Алгоритм:\n"
            "1) Определи, где будет стоп-лосс (по графику).\n"
            "2) Посчитай размер стопа в %.\n"
            "3) Реши, сколько % депозита ты готов рискнуть (например, 1%).\n"
            "4) Риск в $ / стоп в % = объём позиции.\n\n"
            "Пример: депозит 1000$, риск 1% (10$), стоп 5%.\n"
            "10 / 0.05 = 200$ — объём позиции.",

            "📊 <b>Урок 3. Почему без риска любая стратегия умирает</b>\n\n"
            "Даже идеальная точка входа не спасёт, если ты заходишь на весь депозит. "
            "Рынок всегда может сделать движение против тебя.\n\n"
            "Риск-менеджмент — это твоя броня. С ней ты можешь позволить рынку быть ошибочным "
            "несколько раз подряд и всё равно остаться в игре.",

            "📊 <b>Урок 4. Серии сделок и математика профита</b>\n\n"
            "Думай сериями, а не отдельными сделками.\n\n"
            "Если у тебя стратегия с соотношением риск/прибыль 1:2 и винрейт около 40–50%, "
            "то на дистанции ты всё равно выходишь в плюс.\n\n"
            "Задача — не угадать каждый вход, а стабильно реализовывать своё преимущество.",
        ],
    ),
    "crypto_system": (
        "Модуль 3. Торговая система",
        [
            "📈 <b>Урок 1. Из чего состоит рабочая стратегия</b>\n\n"
            "Любая система включает:\n"
            "• понятные условия входа;\n"
            "• условия выхода в плюс и в минус;\n"
            "• риск-менеджмент;\n"
            "• время, когда ты торгуешь.\n\n"
            "Без этих четырёх пунктов это не стратегия, а игра в угадайку.",

            "📈 <b>Урок 2. Работа по тренду</b>\n\n"
            "Мы не ловим ножи и не пытаемся угадать разворот. "
            "Наша задача — встать в сторону уже идущего движения и забрать самый понятный кусок.\n\n"
            "Тренд: серия более высоких максимумов и минимумов (бычий) или ниже-низов и ниже-максимумов (медвежий).",

            "📈 <b>Урок 3. Логика базового сетапа</b>\n\n"
            "1) Определяем тренд на старшем ТФ.\n"
            "2) Ждём откат против тренда.\n"
            "3) Входим в сторону тренда с понятным стопом.\n\n"
            "Это банально, но именно такие простые вещи и работают в реальности.",

            "📈 <b>Урок 4. Домашка по системе</b>\n\n"
            "Открой график любой монеты и найди:\n"
            "• где был сформирован тренд;\n"
            "• где были откаты;\n"
            "• где вход по тренду выглядел бы логичным.\n\n"
            "Задача — натренировать глаз, чтобы в реальной торговле ты мгновенно узнавал знакомые ситуации.",
        ],
    ),
    "traffic": (
        "Модуль 4. Перелив трафика из TikTok в Telegram",
        [
            "🚀 <b>Урок 1. Твоя задача в TikTok</b>\n\n"
            "Главная цель контента — не просто набить просмотры, а привести людей в твой Telegram-бот.\n\n"
            "Там они получают обучение, видят твой продукт и могут купить доступ так же, как это сделал ты.",

            "🚀 <b>Урок 2. Что должно быть в профиле</b>\n\n"
            "• Чёткий аватар (асссоциация с темой денег/крипты).\n"
            "• Понятное описание: кто ты и чем полезен.\n"
            "• Призыв перейти по ссылке в био.\n\n"
            "Человек за 3 секунды должен понять: \"Здесь про деньги и крипту, мне это интересно\".",

            "🚀 <b>Урок 3. Какой контент заходит лучше всего</b>\n\n"
            "Лучше всего работают ролики, где:\n"
            "• показываешь путь — от нуля до первых результатов;\n"
            "• разбираешь типичные ошибки новичков в крипте;\n"
            "• даёшь простые, применимые советы.\n\n"
            "В конце видео всегда приглашай продолжить в Telegram — ссылка в профиле.",

            "🚀 <b>Урок 4. Как прогревать людей в Telegram</b>\n\n"
            "Когда человек заходит в бота по твоей ссылке, он видит не просто продающий текст, "
            "а целую систему: обучение, сигналы, партнёрку.\n\n"
            "Твоя задача — честно показывать, что здесь можно сначала научиться, отбить свои 100$, "
            "а потом выйти в плюс за счёт рекомендаций.",
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
    kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="back_training"))
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


@dp.callback_query_handler(lambda c: c.data == "back_training")
async def cb_back_training(call: types.CallbackQuery):
    try:
        await call.message.answer(
            "🎓 <b>Меню обучения</b>\n\n"
            "Выбери действие:",
            reply_markup=training_menu_keyboard(),
        )
    except Exception as e:
        logging.exception("back_training error: %s", e)
    await call.answer()
    

@dp.callback_query_handler(lambda c: c.data == "train_structure")
async def cb_train_structure(call: types.CallbackQuery):
    text_lines = ["📚 <b>Структура курса:</b>\n"]
    for _key, (title, lessons) in COURSE.items():
        text_lines.append(f"• {title} — {len(lessons)} урок(ов)")
    text_lines.append("\nНажми «Продолжить обучение», чтобы вернуться к своему месту.")

    try:
        # вместо edit_text просто отправляем новое сообщение
        await call.message.answer("\n".join(text_lines), reply_markup=modules_keyboard())
    except Exception as e:
        logging.exception("train_structure error: %s", e)
    await call.answer()


@dp.callback_query_handler(lambda c: c.data == "train_start")
async def cb_train_start(call: types.CallbackQuery):
    user_id = call.from_user.id
    module_key, lesson_index = get_progress(user_id)

    if not module_key or module_key not in COURSE:
        module_key = list(COURSE.keys())[0]
        lesson_index = 0

    await send_lesson(call.message, user_id, module_key, lesson_index)
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("module:"))
async def cb_module(call: types.CallbackQuery):
    _, module_key, _ = call.data.split(":")
    await send_lesson(call.message, call.from_user.id, module_key, 0)
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("lesson:"))
async def cb_lesson(call: types.CallbackQuery):
    _, module_key, idx = call.data.split(":")
    index = int(idx)
    await send_lesson(call.message, call.from_user.id, module_key, index)
    await call.answer()


async def send_lesson(message: types.Message, user_id: int, module_key: str, index: int):
    if module_key not in COURSE:
        return
    title, lessons = COURSE[module_key]
    index = max(0, min(index, len(lessons) - 1))
    last = index == len(lessons) - 1
    header = f"🎓 <b>{title}</b>\nУрок {index + 1} из {len(lessons)}\n\n"
    text = header + lessons[index]
    kb = lesson_nav_keyboard(module_key, index, last)

    set_progress(user_id, module_key, index)

    # всегда отправляем НОВОЕ сообщение, не редактируем старое
    await message.answer(text, reply_markup=kb)



@dp.callback_query_handler(lambda c: c.data == "back_main")
async def cb_back_main(call: types.CallbackQuery):
    user = get_user_by_tg(call.from_user.id)
    has_package = bool(user[7]) if user else False
    await call.message.answer("Главное меню обновлено 👇", reply_markup=back_main_inline())
    await call.message.answer("Выбери нужный раздел:", reply_markup=main_menu(has_package))
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
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
