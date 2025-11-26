import os
import logging
import sqlite3
from datetime import datetime, timedelta

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
# НАСТРОЙКИ
# ---------------------------------------------------------------------------

BOT_TOKEN = "8330326273:AAEw5wkqi7rypz1LZL4LXRr2j5MpKjGc36k"
ADMIN_ID = 682938643
SUPPORT_CONTACT = "@support"  # или твой логин поддержки

PRICE_USD = 100  # стоимость продукта в долларах
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
            reg_date TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            product_code TEXT,
            amount REAL,
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


def get_or_create_user(message: types.Message, referrer_id: int = None):
    user_id = message.from_user.id
    username = message.from_user.username or ""
    first_name = message.from_user.first_name or ""

    conn = db_connect()
    cur = conn.cursor()

    cur.execute("SELECT id, referrer_id FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()

    if row:
        user_db_id, existing_referrer = row
        # если пользователь уже есть, реферера не перезаписываем
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


def get_user_by_user_id(user_id: int):
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


def create_purchase(user_id: int, product_code: str, amount: float) -> int:
    conn = db_connect()
    cur = conn.cursor()
    created_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        "INSERT INTO purchases (user_id, product_code, amount, status, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, product_code, amount, "pending", created_at),
    )
    conn.commit()
    purchase_id = cur.lastrowid
    conn.close()
    return purchase_id


def get_last_pending_purchase(user_id: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, product_code, amount, status, created_at, tx_id FROM purchases "
        "WHERE user_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1",
        (user_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row


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
    cur.execute(
        "SELECT module_key, lesson_index FROM progress WHERE user_id = ?",
        (user_id,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None, 0
    return row[0], row[1]


# ---------------------------------------------------------------------------
# АНТИСПАМ
# ---------------------------------------------------------------------------

user_last_action = {}  # type: dict[int, datetime]
ANTISPAM_SECONDS = 1.2  # минимальный интервал между сообщениями


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

# ключ -> (название модуля, [уроки])
COURSE = {
    "mindset": (
        "Модуль 1. Психология трейдинга",
        [
            "💡 <b>Урок 1. Кто такой трейдер и за что он получает деньги</b>\n\n"
            "Трейдер – это не угадайщик курса. Это человек, который системно принимает решения в условиях "
            "неопределённости и управляет риском. Тебе не нужно быть гением – достаточно дисциплины и понятной "
            "рабочей стратегии.",

            "💡 <b>Урок 2. Почему 90% сливают депозит</b>\n\n"
            "Главные причины: азарт, желание «отбиться», торговля без плана и риски «на всё плечо».\n"
            "Наша задача – сделать из тебя хладнокровного исполнителя своей стратегии, а не игрока в казино.",

            "💡 <b>Урок 3. Правило одной сделки</b>\n\n"
            "Представь, что у тебя осталась одна единственная сделка в жизни. Зайдёшь ли ты в неё прямо сейчас? "
            "Если ответ «нет» – значит вход плохой. Это простой фильтр, который спасает от импульсивных действий.",
        ],
    ),
    "risk": (
        "Модуль 2. Риск-менеджмент",
        [
            "📊 <b>Урок 1. Сколько можно рисковать в одной сделке</b>\n\n"
            "Золотое правило – не более 1–2% от депозита в одной сделке. Так даже серия убыточных входов не убьёт "
            "счёт и даст возможность «вытащить» его за счёт следующих сделок.",

            "📊 <b>Урок 2. Как считать объём позиции</b>\n\n"
            "1) Определи размер стоп-лосса в %.\n"
            "2) Реши, сколько % от депозита ты готов потерять.\n"
            "3) Делим риск на размер стопа – получаем объём позиции.\n\n"
            "Пример: депозит 1000$, риск 1% (10$), стоп 5%. 10 / 0.05 = 200$ – твой объём сделки.",

            "📊 <b>Урок 3. Легенда про «разгон депозита»</b>\n\n"
            "Красивые скрины разгона счёта – почти всегда маркетинг. Реальный трейдинг – это серия аккуратных "
            "повторяющихся действий, а не случайный «выстрел».",
        ],
    ),
    "strategy": (
        "Модуль 3. Торговая система",
        [
            "📈 <b>Урок 1. Из чего состоит стратегия</b>\n\n"
            "Любая рабочая система включает:\n"
            "• условия входа\n"
            "• условия выхода\n"
            "• управление риском\n"
            "• понятное время для торговли.\n\n"
            "Всё остальное – детали реализации.",

            "📈 <b>Урок 2. Наша базовая идея</b>\n\n"
            "Мы работаем по тренду и забираем самые понятные участки движения. Без угадывания разворотов и игры "
            "против сильного движения.",

            "📈 <b>Урок 3. Домашка</b>\n\n"
            "Открой график любой монеты и попробуй глазами найти места, где тренд уже сформирован, а вход в продолжение "
            "движения был бы логичным. Привыкай думать категориями вероятностей.",
        ],
    ),
}

# ---------------------------------------------------------------------------
# КЛАВИАТУРЫ
# ---------------------------------------------------------------------------


def main_menu():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton("🎓 Обучение трейдингу"))
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


def training_menu_keyboard():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("▶️ Начать / продолжить обучение", callback_data="train_start"))
    kb.add(InlineKeyboardButton("📚 Структура курса", callback_data="train_structure"))
    kb.add(InlineKeyboardButton("⬅️ В главное меню", callback_data="back_main"))
    return kb


def modules_keyboard():
    kb = InlineKeyboardMarkup()
    for key, (title, _lessons) in COURSE.items():
        kb.add(InlineKeyboardButton(title, callback_data=f"module:{key}:0"))
    kb.add(InlineKeyboardButton("⬅️ Назад в обучение", callback_data="back_training"))
    return kb


def lesson_nav_keyboard(module_key: str, index: int, last: bool):
    kb = InlineKeyboardMarkup()
    if index > 0:
        kb.insert(InlineKeyboardButton("⬅️ Назад", callback_data=f"lesson:{module_key}:{index - 1}"))
    if not last:
        kb.insert(InlineKeyboardButton("Дальше ▶️", callback_data=f"lesson:{module_key}:{index + 1}"))
    kb.add(InlineKeyboardButton("🏁 Меню обучения", callback_data="back_training"))
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
# ХЭНДЛЕРЫ
# ---------------------------------------------------------------------------


@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    if is_spam(message.from_user.id):
        return

    # парсим реферальный код
    args = message.get_args() or ""
    referrer_id = None
    if args.startswith("ref_"):
        try:
            referrer_tg_id = int(args.split("_", 1)[1])
            if referrer_tg_id != message.from_user.id:
                # найдём referrer в БД
                conn = db_connect()
                cur = conn.cursor()
                cur.execute("SELECT id FROM users WHERE user_id = ?", (referrer_tg_id,))
                row = cur.fetchone()
                conn.close()
                if row:
                    referrer_id = row[0]
        except Exception:
            pass

    get_or_create_user(message, referrer_id)

    me = await bot.get_me()
    ref_link = f"https://t.me/{me.username}?start=ref_{message.from_user.id}"

    text = (
        "👋 <b>Добро пожаловать в TradeX Partner Bot!</b>\n\n"
        "Здесь ты получишь:\n"
        "• Обучение трейдингу с нуля до уверенного понимания рынка.\n"
        "• Закрытые сигналы по торговле.\n"
        "• Пошаговый разбор, как переливать трафик из TikTok в Telegram.\n"
        "• Двухуровневую партнёрку: <b>50%</b> с личных продаж и <b>10%</b> со второго уровня.\n\n"
        "Твоя личная реферальная ссылка:\n"
        f"<code>{ref_link}</code>\n\n"
        "Выбирай нужный раздел в меню 👇"
    )

    await message.answer(text, reply_markup=main_menu())


@dp.message_handler(lambda m: m.text == "📩 Поддержка")
async def support_handler(message: types.Message):
    if is_spam(message.from_user.id):
        return
    await message.answer(
        f"Если возникли вопросы по оплате или работе бота – пиши в поддержку: {SUPPORT_CONTACT}",
        reply_markup=main_menu(),
    )


# -------------------- ОБУЧЕНИЕ -------------------- #


@dp.message_handler(lambda m: m.text == "🎓 Обучение трейдингу")
async def training_menu(message: types.Message):
    if is_spam(message.from_user.id):
        return
    await message.answer(
        "🎓 <b>Обучение трейдингу</b>\n\n"
        "Это пошаговый курс, который можно проходить в удобном темпе. "
        "Каждый модуль раскрывает отдельный блок: психология, риск-менеджмент, сама стратегия.\n\n"
        "Выбери действие:",
        reply_markup=training_menu_keyboard(),
    )


@dp.callback_query_handler(lambda c: c.data == "back_training")
async def cb_back_training(call: CallbackQuery):
    await call.message.edit_text(
        "🎓 <b>Обучение трейдингу</b>\n\n"
        "Выбери действие:",
        reply_markup=training_menu_keyboard(),
    )
    await call.answer()


@dp.callback_query_handler(lambda c: c.data == "train_structure")
async def cb_train_structure(call: CallbackQuery):
    text_lines = ["📚 <b>Структура курса:</b>\n"]
    for _title_key, (title, lessons) in COURSE.items():
        text_lines.append(f"• {title} — {len(lessons)} урок(ов)")
    text_lines.append("\nНажми «Начать / продолжить обучение», чтобы перейти к урокам.")
    await call.message.edit_text("\n".join(text_lines), reply_markup=training_menu_keyboard())
    await call.answer()


@dp.callback_query_handler(lambda c: c.data == "train_start")
async def cb_train_start(call: CallbackQuery):
    user_id = call.from_user.id
    module_key, lesson_index = get_progress(user_id)

    # если прогресса нет – начинаем с первого модуля
    if not module_key or module_key not in COURSE:
        module_key = list(COURSE.keys())[0]
        lesson_index = 0

    await send_lesson(call.message, user_id, module_key, lesson_index, edit=True)
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("module:"))
async def cb_choose_module(call: CallbackQuery):
    _, module_key, _ = call.data.split(":")
    await send_lesson(call.message, call.from_user.id, module_key, 0, edit=True)
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("lesson:"))
async def cb_lesson_nav(call: CallbackQuery):
    _, module_key, index_str = call.data.split(":")
    index = int(index_str)
    await send_lesson(call.message, call.from_user.id, module_key, index, edit=True)
    await call.answer()


async def send_lesson(message: types.Message, user_id: int, module_key: str, index: int, edit: bool = False):
    if module_key not in COURSE:
        return

    title, lessons = COURSE[module_key]
    index = max(0, min(index, len(lessons) - 1))
    lesson_text = lessons[index]
    header = f"🎓 <b>{title}</b>\nУрок {index + 1} из {len(lessons)}\n\n"

    last = index == len(lessons) - 1
    kb = lesson_nav_keyboard(module_key, index, last)

    # сохраняем прогресс по Telegram ID пользователя
    set_progress(user_id, module_key, index)

    if edit:
        await message.edit_text(header + lesson_text, reply_markup=kb)
    else:
        await message.answer(header + lesson_text, reply_markup=kb)


@dp.callback_query_handler(lambda c: c.data == "back_main")
async def cb_back_main(call: CallbackQuery):
    await call.message.edit_text("Главное меню обновлено 👇", reply_markup=back_main_inline())
    # и просто отправим отдельным сообщением меню
    await call.message.answer("Выбери нужный раздел:", reply_markup=main_menu())
    await call.answer()


# -------------------- ПРОДУКТ И ОПЛАТА -------------------- #


@dp.message_handler(lambda m: m.text in ["💼 Комбо: обучение + сигналы", "📈 Сигналы по торговле"])
async def combo_product(message: types.Message):
    if is_spam(message.from_user.id):
        return

    user_row = get_user_by_user_id(message.from_user.id)
    if not user_row:
        get_or_create_user(message)

    description = (
        "💼 <b>Комбо-продукт: обучение + сигналы</b>\n\n"
        "Что входит:\n"
        "• Полное обучение трейдингу внутри бота.\n"
        "• Доступ к закрытым сигналам по торговле.\n"
        "• Обучение по переливу трафика из TikTok в Telegram.\n\n"
        f"Стоимость доступа: <b>{PRICE_USD}$</b> (единожды).\n\n"
        "После оплаты ты получаешь пожизненный доступ к материалам и можешь "
        "зарабатывать по партнёрке: 50% с личных продаж и 10% со второго уровня."
    )

    user_db_row = get_user_by_user_id(message.from_user.id)
    if not user_db_row:
        user_db_id = get_or_create_user(message)
    else:
        user_db_id = user_db_row[0]

    purchase_id = create_purchase(user_db_id, "combo", PRICE_USD)

    pay_text = (
        description
        + "\n\n<b>Как оплатить:</b>\n"
          "1. Переведи <b>100$</b> на реквизиты, которые тебе даст админ или бот (USDT, карта и т.д.).\n"
          "2. Обязательно укажи в комментарии/примечании слово: "
        f"<code>TX{purchase_id}</code>\n"
        "3. После перевода нажми кнопку «Я оплатил» ниже.\n\n"
        "Админ сверит транзакцию и бот автоматически выдаст доступ."
    )

    await message.answer(pay_text, reply_markup=pay_keyboard(purchase_id))


@dp.callback_query_handler(lambda c: c.data.startswith("paid:"))
async def cb_paid(call: CallbackQuery):
    """
    Пользователь нажал «Я оплатил».
    """
    _, purchase_id_str = call.data.split(":")
    purchase_id = int(purchase_id_str)

    # найдём покупку
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT p.id, p.user_id, u.user_id, u.username, u.first_name, p.amount, p.status "
        "FROM purchases p JOIN users u ON p.user_id = u.id WHERE p.id = ?",
        (purchase_id,),
    )
    row = cur.fetchone()
    conn.close()

    if not row:
        await call.answer("Заявка на оплату не найдена. Напиши в поддержку.", show_alert=True)
        return

    _, user_db_id, tg_id, username, first_name, amount, status = row

    if status == "paid":
        await call.answer("Эта оплата уже подтверждена ✅", show_alert=True)
        return

    user_mention = f"<a href='tg://user?id={tg_id}'>{first_name}</a>"
    uname = f"@{username}" if username else ""

    text_for_admin = (
        "💳 <b>Новая заявка на оплату</b>\n\n"
        f"Пользователь: {user_mention} {uname}\n"
        f"Telegram ID: <code>{tg_id}</code>\n"
        f"ID записи в БД: <code>{user_db_id}</code>\n"
        f"Сумма: <b>{amount}$</b>\n"
        f"ID покупки: <code>{purchase_id}</code>\n\n"
        "Если оплата пришла – нажми кнопку ниже, и бот сам начислит партнёрские."
    )

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("✅ Подтвердить оплату", callback_data=f"confirm:{purchase_id}"))

    await bot.send_message(ADMIN_ID, text_for_admin, reply_markup=kb)
    await call.message.answer(
        "✅ Заявка отправлена администратору.\n\n"
        "Как только оплата будет подтверждена, бот выдаст доступ и начислит бонусы по партнёрке.",
        reply_markup=main_menu(),
    )
    await call.answer("Заявка отправлена админу", show_alert=True)


@dp.callback_query_handler(lambda c: c.data.startswith("confirm:"), user_id=ADMIN_ID)
async def cb_confirm_payment(call: CallbackQuery):
    """
    Админ нажимает кнопку подтверждения.
    """
    _, purchase_id_str = call.data.split(":")
    purchase_id = int(purchase_id_str)

    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT p.id, p.user_id, u.user_id, u.first_name, p.amount, p.status "
        "FROM purchases p JOIN users u ON p.user_id = u.id WHERE p.id = ?",
        (purchase_id,),
    )
    row = cur.fetchone()

    if not row:
        await call.answer("Покупка не найдена", show_alert=True)
        conn.close()
        return

    _, user_db_id, buyer_tg_id, buyer_first_name, amount, status = row

    if status == "paid":
        await call.answer("Уже подтверждено ✅", show_alert=True)
        conn.close()
        return

    # помечаем как оплачено
    mark_purchase_paid(purchase_id, tx_id="manual_admin_confirm")

    # реферальные начисления
    lvl1_id, lvl2_id = get_referrer_chain(user_db_id)

    lvl1_bonus = amount * LEVEL1_PERCENT
    lvl2_bonus = amount * LEVEL2_PERCENT

    if lvl1_id:
        add_balance(lvl1_id, lvl1_bonus)

    if lvl2_id:
        add_balance(lvl2_id, lvl2_bonus)

    conn.close()

    # уведомляем покупателя
    try:
        await bot.send_message(
            buyer_tg_id,
            "✅ <b>Оплата подтверждена!</b>\n\n"
            "Доступ к обучению и сигналам открыт. Кнопки для перехода в разделы уже доступны в главном меню.",
            reply_markup=main_menu(),
        )
    except Exception:
        pass

    # уведомляем рефералов
    if lvl1_id:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE id = ?", (lvl1_id,))
        r1 = cur.fetchone()
        conn.close()
        if r1:
            lvl1_tg = r1[0]
            try:
                await bot.send_message(
                    lvl1_tg,
                    f"💰 <b>Начислено {lvl1_bonus}$</b> за личную рекомендацию.\n"
                    f"Твой партнёр {buyer_first_name} совершил покупку на {amount}$."
                )
            except Exception:
                pass

    if lvl2_id:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE id = ?", (lvl2_id,))
        r2 = cur.fetchone()
        conn.close()
        if r2:
            lvl2_tg = r2[0]
            try:
                await bot.send_message(
                    lvl2_tg,
                    f"💸 <b>Начислено {lvl2_bonus}$</b> со второго уровня.\n"
                    f"Партнёр второго уровня совершил покупку на {amount}$."
                )
            except Exception:
                pass

    await call.answer("Оплата подтверждена, бонусы начислены ✅", show_alert=True)
    await call.message.edit_reply_markup()  # убираем кнопки под заявкой


# -------------------- ПАРТНЁРКА И СТАТИСТИКА -------------------- #


@dp.message_handler(lambda m: m.text == "👥 Партнёрская программа")
async def partners_handler(message: types.Message):
    if is_spam(message.from_user.id):
        return

    user = get_user_by_user_id(message.from_user.id)
    if not user:
        get_or_create_user(message)
        user = get_user_by_user_id(message.from_user.id)

    user_db_id, _, username, first_name, referrer_id, balance, total_earned = user

    me = await bot.get_me()
    ref_link = f"https://t.me/{me.username}?start=ref_{message.from_user.id}"

    text = (
        "👥 <b>Партнёрская программа TradeX</b>\n\n"
        "Ты можешь зарабатывать на рекомендациях нашего продукта:\n"
        f"• <b>50%</b> с каждой продажи по твоей ссылке.\n"
        f"• <b>10%</b> с продаж партнёров второго уровня.\n\n"
        "Пример:\n"
        "Ты привёл друга – он купил доступ за 100$ → ты получил 50$.\n"
        "Друг привёл ещё человека → он получил 50$, а ты +10$ сверху.\n\n"
        "Твоя личная ссылка:\n"
        f"<code>{ref_link}</code>\n\n"
        f"Текущий баланс к выводу: <b>{balance}$</b>\n"
        f"Всего заработано за всё время: <b>{total_earned}$</b>\n\n"
        "Вывод средств делается через администратора. Напиши в поддержку, когда захочешь вывести прибыль."
    )

    await message.answer(text, reply_markup=main_menu())


@dp.message_handler(lambda m: m.text == "📊 Моя статистика")
async def my_stats(message: types.Message):
    if is_spam(message.from_user.id):
        return

    user = get_user_by_user_id(message.from_user.id)
    if not user:
        await message.answer(
            "Пока нет данных. Нажми /start, чтобы зарегистрироваться.",
            reply_markup=main_menu(),
        )
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
        f"Баланс к выводу: <b>{balance}$</b>\n"
        f"Всего заработано: <b>{total_earned}$</b>\n\n"
        "Продолжай делиться ссылкой и зарабатывай больше 💸"
    )

    await message.answer(text, reply_markup=main_menu())


# -------------------- ПРОЧЕЕ -------------------- #


@dp.message_handler()
async def fallback(message: types.Message):
    if is_spam(message.from_user.id):
        return
    await message.answer("Не понял сообщение 🤔\nВыбери пункт в меню ниже.", reply_markup=main_menu())


# ---------------------------------------------------------------------------
# ЗАПУСК
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан в переменных окружения")
    init_db()
    executor.start_polling(dp, skip_updates=True)
