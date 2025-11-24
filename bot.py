import os
import asyncio
import logging
import random
import sqlite3
from datetime import datetime, timedelta

import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.dispatcher.filters import Text, Command
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)

# ==========================
# НАСТРОЙКИ / КОНФИГ
# ==========================

# Можно взять из переменных окружения (рекомендуется на хостинге)
BOT_TOKEN = os.getenv("BOT_TOKEN", "8330326273:AAEwSwkqi7ypz1LZL4LXRr2jSMpKjGc36k")
ADMIN_ID = int(os.getenv("ADMIN_ID", "682938643"))  # замени на свой ID

TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "b33b8d65-10c9-4f7b-99e0-ab47f3bbbb60")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "TSY9xF24bQ3Kbd1Njp2w4pEEoqJow1nfpr")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1003464806734"))  # ID закрытого канала

PRICE_USDT = float(os.getenv("PRICE_USDT", "100"))  # цена подписки за месяц
SUB_DAYS = int(os.getenv("SUB_DAYS", "30"))         # длительность подписки в днях

DB_PATH = "database.db"

EXPIRE_CHECK_INTERVAL = 1800  # 30 минут
PAYMENT_SCAN_INTERVAL = 60    # 1 минута

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# ==========================
# БАЗА ДАННЫХ
# ==========================

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

# Базовое создание таблиц
cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_seen TEXT,
        last_active TEXT
    );
    """
)

cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS subscriptions(
        user_id INTEGER PRIMARY KEY,
        unique_price REAL,
        paid INTEGER,
        start_date TEXT,
        end_date TEXT,
        tx_amount REAL,
        tx_time TEXT
    );
    """
)

conn.commit()

# --- МИГРАЦИИ ДЛЯ users (добавляем столбцы, если их нет) ---

cursor.execute("PRAGMA table_info(users)")
user_cols = [row[1] for row in cursor.fetchall()]

if "referrer_id" not in user_cols:
    cursor.execute("ALTER TABLE users ADD COLUMN referrer_id INTEGER")

if "utm_tag" not in user_cols:
    cursor.execute("ALTER TABLE users ADD COLUMN utm_tag TEXT")

if "last_module" not in user_cols:
    cursor.execute("ALTER TABLE users ADD COLUMN last_module TEXT")

if "last_lesson" not in user_cols:
    cursor.execute("ALTER TABLE users ADD COLUMN last_lesson INTEGER")

conn.commit()

# --- МИГРАЦИИ ДЛЯ subscriptions ---

cursor.execute("PRAGMA table_info(subscriptions)")
sub_cols = [row[1] for row in cursor.fetchall()]

if "unique_price" not in sub_cols:
    cursor.execute("ALTER TABLE subscriptions ADD COLUMN unique_price REAL")

if "paid" not in sub_cols:
    cursor.execute("ALTER TABLE subscriptions ADD COLUMN paid INTEGER")

if "start_date" not in sub_cols:
    cursor.execute("ALTER TABLE subscriptions ADD COLUMN start_date TEXT")

if "end_date" not in sub_cols:
    cursor.execute("ALTER TABLE subscriptions ADD COLUMN end_date TEXT")

if "tx_amount" not in sub_cols:
    cursor.execute("ALTER TABLE subscriptions ADD COLUMN tx_amount REAL")

if "tx_time" not in sub_cols:
    cursor.execute("ALTER TABLE subscriptions ADD COLUMN tx_time TEXT")

conn.commit()

# Временное хранение уникальных сумм для оплаты
user_unique_price: dict[int, float] = {}

# ==========================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def save_user(user_id: int, username: str | None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    cursor.execute(
        """
        INSERT INTO users (user_id, username, first_seen, last_active)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET last_active = excluded.last_active
        """,
        (user_id, username or "", now, now),
    )
    conn.commit()


def get_subscription(user_id: int):
    cursor.execute(
        """
        SELECT user_id, unique_price, paid, start_date, end_date, tx_amount, tx_time
        FROM subscriptions
        WHERE user_id = ?
        """,
        (user_id,),
    )
    return cursor.fetchone()


def save_payment(user_id: int, unique_price: float, tx_amount: float):
    now = datetime.now()
    end = now + timedelta(days=SUB_DAYS)
    cursor.execute(
        """
        INSERT OR REPLACE INTO subscriptions
        (user_id, unique_price, paid, start_date, end_date, tx_amount, tx_time)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            unique_price,
            1,
            now.strftime("%Y-%m-%d %H:%M"),
            end.strftime("%Y-%m-%d %H:%M"),
            tx_amount,
            now.strftime("%Y-%m-%d %H:%M"),
        ),
    )
    conn.commit()


def set_paid(user_id: int, paid: int):
    cursor.execute("UPDATE subscriptions SET paid = ? WHERE user_id = ?", (paid, user_id))
    conn.commit()


def save_training_progress(user_id: int, module_key: str, lesson_index: int):
    cursor.execute(
        """
        UPDATE users SET last_module = ?, last_lesson = ?
        WHERE user_id = ?
        """,
        (module_key, lesson_index, user_id),
    )
    conn.commit()


def get_training_progress(user_id: int):
    cursor.execute(
        "SELECT last_module, last_lesson FROM users WHERE user_id = ?",
        (user_id,),
    )
    row = cursor.fetchone()
    if row and row[0] is not None:
        return row[0], row[1]
    return None, None


async def log_to_admin(text: str):
    try:
        await bot.send_message(ADMIN_ID, f"🛠 LOG:\n{text}")
    except Exception as e:
        logging.error(f"Не удалось отправить лог админу: {e}")


# ==========================
# ПРОВЕРКА ОПЛАТЫ TRONGRID
# ==========================

async def check_trx_payment(user_id: int) -> bool:
    """
    Проверяем, пришёл ли USDT (TRC-20) с нужной уникальной суммой.
    """
    target_amount = user_unique_price.get(user_id)
    if target_amount is None:
        return False

    url = f"https://api.trongrid.io/v1/accounts/{WALLET_ADDRESS}/transactions/trc20"
    headers = {"TRON-PRO-API-KEY": TRONGRID_API_KEY}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                data = await resp.json()
    except Exception as e:
        logging.error(f"Ошибка запроса TronGrid: {e}")
        return False

    for tx in data.get("data", []):
        try:
            raw_value = tx.get("value") or tx.get("amount")
            if raw_value is None:
                continue
            amount = int(raw_value) / 1_000_000  # USDT с 6 знаками
            if abs(amount - target_amount) < 0.0000001:
                return True
        except Exception:
            continue

    return False


# ==========================
# КЛАВИАТУРЫ
# ==========================

def main_keyboard() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(
        KeyboardButton("📌 О боте"),
        KeyboardButton("📈 Получить сигналы")
    )
    kb.row(
        KeyboardButton("💰 Тарифы"),
        KeyboardButton("📞 Поддержка")
    )
    kb.row(
        KeyboardButton("👤 Профиль"),
        KeyboardButton("🎓 Обучение трейдингу")
    )
    return kb


def admin_keyboard() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton("👥 Все пользователи"))
    kb.row(KeyboardButton("📊 Все подписчики"))
    kb.row(KeyboardButton("🔥 Активные подписчики"))
    kb.row(KeyboardButton("⏳ Истёкшие"))
    kb.row(KeyboardButton("🧾 История платежей"))
    kb.row(KeyboardButton("📤 Экспорт CSV"))
    return kb


# ==========================
# ОБУЧЕНИЕ (TradeX Academy)
# ==========================

TRAINING_COURSE = {
    "module1": {
        "title": "Модуль 1 — Основы криптовалют и рынка",
        "lessons": [
            {
                "title": "1.1 Что такое криптовалюта и рынок",
                "text": (
                    "💡 <b>Что такое рынок и криптовалюта</b>\n\n"
                    "Рынок — это место, где одни люди покупают, а другие продают. В крипте всё то же самое, "
                    "только вместо магазинов — биржи.\n\n"
                    "Криптовалюта — это цифровые деньги, которые существуют в блокчейне. У них нет бумажных "
                    "банкнот, но ими можно платить, инвестировать и торговать.\n\n"
                    "Главные идеи:\n"
                    "• Любой рынок живёт за счёт дисбаланса спроса и предложения.\n"
                    "• Цена растёт, когда покупают больше, чем продают.\n"
                    "• Цена падает, когда продают больше, чем покупают.\n\n"
                    "Трейдер зарабатывает на движении цены — вверх или вниз. Наша задача — научиться понимать, "
                    "где шансы на прибыль выше, а риск — контролируемый."
                ),
            },
            {
                "title": "1.2 Биржи, стакан и ликвидность",
                "text": (
                    "🏦 <b>Криптобиржа, стакан и ликвидность</b>\n\n"
                    "Криптобиржа — платформа, где встречаются покупатели и продавцы. Пример: Binance, Bybit и др.\n\n"
                    "Основные понятия:\n"
                    "• <b>Стакан</b> — список заявок на покупку и продажу по разным ценам.\n"
                    "• <b>Ликвидность</b> — насколько легко войти и выйти из сделки без сильного проскальзывания.\n"
                    "• <b>Спред</b> — разница между лучшей ценой покупки и лучшей ценой продажи.\n\n"
                    "Чем больше ликвидность — тем спокойнее и предсказуемее движение цены. "
                    "Торгуя малоликвидные монеты, ты рискуешь попасть в резкий вынос и большую просадку."
                ),
            },
            {
                "title": "1.3 Виды торговли: спот и фьючерсы",
                "text": (
                    "📊 <b>Спот vs Фьючерсы</b>\n\n"
                    "• <b>Спот</b> — ты покупаешь монету и реально владеешь ею. Заработок идёт, если цена растёт. "
                    "Риск — ограничен суммой покупки.\n\n"
                    "• <b>Фьючерсы</b> — торговля контрактами с плечом. Можно зарабатывать как на росте, так и на падении.\n\n"
                    "Важно:\n"
                    "• Плечо усиливает и прибыль, и убыток.\n"
                    "• Неправильное использование фьючерсов — самый быстрый путь слить депозит.\n\n"
                    "Новичку безопаснее начинать со спота и только потом постепенно переходить к фьючерсам с "
                    "минимальными плечами и чётким риск-менеджментом."
                ),
            },
        ],
    },
    "module2": {
        "title": "Модуль 2 — Технический анализ: графики и уровни",
        "lessons": [
            {
                "title": "2.1 Свечи и таймфреймы",
                "text": (
                    "📉 <b>Свечи и таймфреймы</b>\n\n"
                    "График в виде свечей — это основа технического анализа:\n"
                    "• Каждая свеча показывает движение цены за выбранный промежуток (таймфрейм).\n"
                    "• У свечи есть тело (между ценой открытия и закрытия) и тени (минимум и максимум).\n\n"
                    "Таймфреймы:\n"
                    "• M1, M5, M15 — скальпинг, короткие сделки.\n"
                    "• H1, H4 — среднесрочная торговля.\n"
                    "• D1, W1 — общая картина рынка.\n\n"
                    "Ключевой принцип: <b>анализ всегда начинается с больших таймфреймов</b>, "
                    "а вход ищется на меньших."
                ),
            },
            {
                "title": "2.2 Уровни поддержки и сопротивления",
                "text": (
                    "📏 <b>Уровни поддержки и сопротивления</b>\n\n"
                    "• <b>Поддержка</b> — область, где цену раньше активно выкупали.\n"
                    "• <b>Сопротивление</b> — область, где цену раньше активно продавали.\n\n"
                    "Как использовать:\n"
                    "• Покупают ближе к поддержке, продают/шортят ближе к сопротивлению.\n"
                    "• Пробой уровня с объёмом часто даёт сильное движение.\n\n"
                    "Не рисуй уровни слишком часто — концентрируйся на зонах, где цена явно реагировала несколько раз."
                ),
            },
            {
                "title": "2.3 Тренды и каналы",
                "text": (
                    "📈 <b>Тренд и торговые каналы</b>\n\n"
                    "• Восходящий тренд — серия более высоких минимумов и максимумов.\n"
                    "• Нисходящий тренд — серия более низких максимумов и минимумов.\n\n"
                    "Тренд — твой союзник:\n"
                    "• Проще и безопаснее торговать по тренду.\n"
                    "• Восходящий тренд — приоритет лонгов.\n"
                    "• Нисходящий — приоритет шортов.\n\n"
                    "Канал — это тренд с параллельной линией. От границ канала можно искать входы с коротким стопом."
                ),
            },
        ],
    },
    "module3": {
        "title": "Модуль 3 — Индикаторы и фильтрация сделок",
        "lessons": [
            {
                "title": "3.1 RSI: сила движения",
                "text": (
                    "📊 <b>Индикатор RSI</b>\n\n"
                    "RSI показывает, насколько рынок перекуплен или перепродан.\n\n"
                    "Классические уровни:\n"
                    "• Выше 70 — перекупленность (риск коррекции).\n"
                    "• Ниже 30 — перепроданность (риск отскока).\n\n"
                    "Важно:\n"
                    "• Не шортить любой рост только потому, что RSI > 70.\n"
                    "• В тренде RSI может долго быть в зоне перекупленности/перепроданности.\n\n"
                    "Используй RSI как фильтр, а не как единственный сигнал."
                ),
            },
            {
                "title": "3.2 Скользящие средние (MA/EMA)",
                "text": (
                    "📉 <b>Скользящие средние (Moving Averages)</b>\n\n"
                    "Скользящая средняя сглаживает цену и показывает направление тенденции.\n\n"
                    "Популярные периоды:\n"
                    "• 50, 100, 200 — для старших таймфреймов.\n"
                    "• 9, 21 — для внутридневной торговли.\n\n"
                    "Примеры использования:\n"
                    "• Цена выше EMA 200 — преобладает бычий контекст.\n"
                    "• Пересечения EMA 9 и EMA 21 можно использовать как сигнальные точки.\n\n"
                    "Не воспринимай пересечения MA как «магический вход» — всегда смотри на уровни и тренд."
                ),
            },
            {
                "title": "3.3 Объёмы и сила движения",
                "text": (
                    "📊 <b>Объём — топливо движения</b>\n\n"
                    "Объём показывает силу интереса участников.\n\n"
                    "Основные принципы:\n"
                    "• Сильное движение без объёма — слабое и нестабильное.\n"
                    "• Рост цены на растущем объёме — здоровый тренд.\n"
                    "• Памп без объёма — возможный обман.\n\n"
                    "Даже простое чтение гистограммы объёмов даёт тебе преимущество над теми, кто её игнорирует."
                ),
            },
        ],
    },
    "module4": {
        "title": "Модуль 4 — Риск-менеджмент и деньги",
        "lessons": [
            {
                "title": "4.1 Почему сливаются даже умные люди",
                "text": (
                    "💣 <b>Главная причина сливов — отсутствие риск-менеджмента</b>\n\n"
                    "Большинство трейдеров сливают депозит не потому что не умеют анализировать рынок, "
                    "а потому что:\n"
                    "• заходят слишком большим объёмом;\n"
                    "• не ставят стоп;\n"
                    "• усредняются против тренда;\n"
                    "• не считают риск на сделку.\n\n"
                    "Твоя задача — не угадывать рынок, а выжить достаточно долго, чтобы опыт начал приносить прибыль."
                ),
            },
            {
                "title": "4.2 Риск на сделку и размер позиции",
                "text": (
                    "📏 <b>Риск на сделку — фундамент</b>\n\n"
                    "Классическое правило:\n"
                    "• Рисковать 1–2% депозита на одну сделку.\n\n"
                    "Пример:\n"
                    "• Депозит 1000$.\n"
                    "• 2% риска = 20$.\n"
                    "• Стоп = 5% от входа.\n"
                    "• Тогда объём позиции ≈ 400$.\n\n"
                    "Если ты не считаешь риск — рынок посчитает за тебя. Обычно не в твою пользу."
                ),
            },
            {
                "title": "4.3 Серия сделок и матожидание",
                "text": (
                    "🎯 <b>Торговля — это серия сделок, а не одна попытка</b>\n\n"
                    "Важные идеи:\n"
                    "• Оценивай результат не по одной сделке, а по серии (20–50 сделок).\n"
                    "• При строгом риске и адекватных тейках даже 40–50% винрейта могут быть прибыльными.\n\n"
                    "Пример:\n"
                    "• Риск 1R, профит 2R.\n"
                    "• Из 10 сделок 5 в плюс, 5 в минус.\n"
                    "• Итог: +5×2R - 5×1R = +5R.\n\n"
                    "Твоя цель — система, где матожидание в пользу роста депозита."
                ),
            },
        ],
    },
    "module5": {
        "title": "Модуль 5 — Психология трейдера",
        "lessons": [
            {
                "title": "5.1 Эмоции: страх, жадность и FOMO",
                "text": (
                    "🧠 <b>Эмоции — главный враг трейдера</b>\n\n"
                    "Страх и жадность заставляют нарушать правила и ломать стратегию.\n\n"
                    "Типичные ловушки:\n"
                    "• FOMO — страх упустить движение.\n"
                    "• Revenge-trading — попытка отыграться после убытка.\n"
                    "• Овертрейдинг — слишком много сделок подряд.\n\n"
                    "Задача — не отключить эмоции, а не позволять им принимать решения. "
                    "Решения принимает система, эмоции — наблюдают."
                ),
            },
            {
                "title": "5.2 Дисциплина и торговый план",
                "text": (
                    "📋 <b>Торговый план — твоя карта</b>\n\n"
                    "План включает:\n"
                    "• условия входа;\n"
                    "• условия выхода;\n"
                    "• риск на сделку;\n"
                    "• время торгов;\n"
                    "• список запрещённых действий.\n\n"
                    "Каждый вход должен быть по плану. Если сделка не вписывается в правила — это не трейдинг, а казино."
                ),
            },
            {
                "title": "5.3 Как переживать просадки",
                "text": (
                    "🌧 <b>Просадки — неизбежная часть пути</b>\n\n"
                    "Любая стратегия имеет периоды просадок. Главное:\n"
                    "• не увеличивать риск в попытке «отбиться»;\n"
                    "• не менять систему после 2–3 убыточных сделок;\n"
                    "• вести дневник, анализировать ошибки.\n\n"
                    "Сильный трейдер отличается от слабого не отсутствием убытков, а умением переживать их "
                    "без разрушения системы."
                ),
            },
        ],
    },
    "module6": {
        "title": "Модуль 6 — Практика и рабочий подход",
        "lessons": [
            {
                "title": "6.1 Простая рабочая стратегия",
                "text": (
                    "⚙️ <b>Пример базовой рабочей стратегии</b>\n\n"
                    "1) Анализ D1/H4 — определить глобальный контекст (бычий/медвежий/флет).\n"
                    "2) На H1 — найти ключевые уровни поддержки/сопротивления.\n"
                    "3) На M15 — искать вход по откату к уровню в сторону тренда.\n"
                    "4) Стоп — за локальный экстремум.\n"
                    "5) Тейк — минимум 2R (в 2 раза больше риска).\n\n"
                    "Такая структура даёт понятную логику входа и выхода без попытки угадать каждую свечу."
                ),
            },
            {
                "title": "6.2 Как совмещать сигналы и своё обучение",
                "text": (
                    "🤝 <b>Сигналы + собственный анализ</b>\n\n"
                    "Сигналы экономят время и дают идеи, но:\n"
                    "• не отменяют твою ответственность за риск;\n"
                    "• не заменяют навыка чтения графика.\n\n"
                    "Идеальный подход:\n"
                    "• использовать сигналы как список идей;\n"
                    "• проверять уровни, тренд, объёмы;\n"
                    "• входить только там, где ты сам понимаешь логику сделки.\n\n"
                    "Так ты превращаешься не в «подписчика сигналов», а в трейдера, который использует сигналы как инструмент."
                ),
            },
            {
                "title": "6.3 Личный план развития",
                "text": (
                    "🚀 <b>Твой личный план роста</b>\n\n"
                    "1) Пройти весь курс и законспектировать ключевые идеи.\n"
                    "2) Открыть демо/малый депозит и тренироваться системно.\n"
                    "3) Вести дневник сделок (скрины, эмоции, мысли).\n"
                    "4) Раз в неделю анализировать результаты и корректировать план.\n"
                    "5) Не спешить увеличивать объём до тех пор, пока статистика не станет стабильной.\n\n"
                    "Торговля — это марафон. Ты здесь не на один день. "
                    "С правильным подходом трейдинг может стать сильным источником дохода, а не лотереей."
                ),
            },
        ],
    },
}


def training_main_kb(has_progress: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    if has_progress:
        kb.add(InlineKeyboardButton("▶️ Продолжить обучение", callback_data="train_continue"))
    kb.add(InlineKeyboardButton("📚 Выбрать модуль", callback_data="train_modules"))
    return kb


def training_modules_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    for key, module in TRAINING_COURSE.items():
        kb.add(InlineKeyboardButton(module["title"], callback_data=f"train_module:{key}"))
    kb.add(InlineKeyboardButton("⬅️ В главное меню", callback_data="train_back_menu"))
    return kb


def training_lessons_kb(module_key: str, current_index: int | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    lessons = TRAINING_COURSE[module_key]["lessons"]
    for i, lesson in enumerate(lessons):
        prefix = "▶️ " if current_index == i else "📘 "
        kb.add(
            InlineKeyboardButton(
                f"{prefix}{lesson['title']}",
                callback_data=f"train_lesson:{module_key}:{i}",
            )
        )
    kb.add(InlineKeyboardButton("⬅️ К модулям", callback_data="train_modules"))
    return kb


def training_nav_kb(module_key: str, lesson_index: int, total_lessons: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    row = []
    if lesson_index > 0:
        row.append(
            InlineKeyboardButton("⬅️ Назад", callback_data=f"train_prev:{module_key}:{lesson_index}")
        )
    if lesson_index < total_lessons - 1:
        row.append(
            InlineKeyboardButton("➡️ Далее", callback_data=f"train_next:{module_key}:{lesson_index}")
        )
    if row:
        kb.row(*row)
    kb.add(InlineKeyboardButton("📚 К урокам", callback_data=f"train_lessons:{module_key}"))
    kb.add(InlineKeyboardButton("🏠 В раздел обучения", callback_data="train_root"))
    return kb


# ==========================
# ХЕНДЛЕРЫ ОБУЧЕНИЯ
# ==========================

@dp.message_handler(Text(equals=["Обучение трейдингу", "🎓 Обучение трейдингу"]))
async def training_entry(message: types.Message):
    user_id = message.from_user.id
    module_key, lesson_index = get_training_progress(user_id)
    has_progress = module_key is not None
    text = (
        "🎓 <b>TradeX Academy — обучение трейдингу</b>\n\n"
        "Здесь ты получишь структурированные знания:\n"
        "от базовых принципов рынка до практической стратегии, риск-менеджмента и психологии.\n\n"
        "Можешь продолжить с места, где остановился, или выбрать модуль."
    )
    await message.answer(text, reply_markup=training_main_kb(has_progress), parse_mode="HTML")


@dp.callback_query_handler(lambda c: c.data == "train_root")
async def training_root(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    module_key, lesson_index = get_training_progress(user_id)
    has_progress = module_key is not None
    text = (
        "🎓 <b>TradeX Academy</b>\n\n"
        "Выбери, что делать дальше:"
    )
    await callback.message.edit_text(text, reply_markup=training_main_kb(has_progress), parse_mode="HTML")
    await callback.answer()


@dp.callback_query_handler(lambda c: c.data == "train_back_menu")
async def training_back_menu(callback: types.CallbackQuery):
    await callback.message.edit_text("🏠 Возврат в главное меню.", parse_mode="HTML")
    await callback.message.answer("Главное меню:", reply_markup=main_keyboard())
    await callback.answer()


@dp.callback_query_handler(lambda c: c.data == "train_modules")
async def training_show_modules(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "📚 <b>Выбор модуля:</b>\n\nВыбери интересующий раздел:",
        reply_markup=training_modules_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query_handler(lambda c: c.data == "train_continue")
async def training_continue(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    module_key, lesson_index = get_training_progress(user_id)
    if not module_key:
        await callback.answer("Прогресс не найден, выбери модуль.", show_alert=True)
        return

    lessons = TRAINING_COURSE.get(module_key, {}).get("lessons", [])
    if not lessons or lesson_index is None or lesson_index >= len(lessons):
        await callback.answer("Не удалось продолжить, выбери модуль.", show_alert=True)
        return

    lesson = lessons[lesson_index]
    text = f"📘 <b>{lesson['title']}</b>\n\n{lesson['text']}"
    kb = training_nav_kb(module_key, lesson_index, len(lessons))
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("train_module:"))
async def training_open_module(callback: types.CallbackQuery):
    module_key = callback.data.split(":", 1)[1]
    if module_key not in TRAINING_COURSE:
        await callback.answer("Модуль не найден.", show_alert=True)
        return
    title = TRAINING_COURSE[module_key]["title"]
    text = f"📚 <b>{title}</b>\n\nВыбери урок:"
    kb = training_lessons_kb(module_key)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("train_lessons:"))
async def training_lessons_list(callback: types.CallbackQuery):
    module_key = callback.data.split(":", 1)[1]
    if module_key not in TRAINING_COURSE:
        await callback.answer("Модуль не найден.", show_alert=True)
        return
    title = TRAINING_COURSE[module_key]["title"]
    current_module, current_idx = get_training_progress(callback.from_user.id)
    idx = current_idx if current_module == module_key else None
    text = f"📚 <b>{title}</b>\n\nВыбери урок:"
    kb = training_lessons_kb(module_key, idx)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("train_lesson:"))
async def training_open_lesson(callback: types.CallbackQuery):
    _, module_key, idx = callback.data.split(":")
    idx = int(idx)
    lessons = TRAINING_COURSE.get(module_key, {}).get("lessons", [])
    if not lessons or idx >= len(lessons):
        await callback.answer("Урок не найден.", show_alert=True)
        return
    save_training_progress(callback.from_user.id, module_key, idx)
    lesson = lessons[idx]
    text = f"📘 <b>{lesson['title']}</b>\n\n{lesson['text']}"
    kb = training_nav_kb(module_key, idx, len(lessons))
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("train_next:"))
async def training_next(callback: types.CallbackQuery):
    _, module_key, idx = callback.data.split(":")
    idx = int(idx)
    next_idx = idx + 1
    lessons = TRAINING_COURSE.get(module_key, {}).get("lessons", [])
    if next_idx >= len(lessons):
        await callback.answer("Это последний урок в модуле.", show_alert=True)
        return
    save_training_progress(callback.from_user.id, module_key, next_idx)
    lesson = lessons[next_idx]
    text = f"📘 <b>{lesson['title']}</b>\n\n{lesson['text']}"
    kb = training_nav_kb(module_key, next_idx, len(lessons))
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("train_prev:"))
async def training_prev(callback: types.CallbackQuery):
    _, module_key, idx = callback.data.split(":")
    idx = int(idx)
    prev_idx = idx - 1
    if prev_idx < 0:
        await callback.answer("Это первый урок.", show_alert=True)
        return
    lessons = TRAINING_COURSE.get(module_key, {}).get("lessons", [])
    lesson = lessons[prev_idx]
    save_training_progress(callback.from_user.id, module_key, prev_idx)
    text = f"📘 <b>{lesson['title']}</b>\n\n{lesson['text']}"
    kb = training_nav_kb(module_key, prev_idx, len(lessons))
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


# ==========================
# ОБЫЧНЫЕ КОМАНДЫ И МЕНЮ
# ==========================

@dp.message_handler(Command("start"))
async def cmd_start(message: types.Message):
    save_user(message.from_user.id, message.from_user.username)

    row = get_subscription(message.from_user.id)
    now = datetime.now()

    if row:
        _, _, paid, _, end_date, _, _ = row
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d %H:%M")
        except Exception:
            end_dt = now

        if paid == 1 and end_dt > now:
            txt = (
                "🔥 У тебя уже есть активная подписка!\n"
                f"Действует до: <b>{end_date}</b>\n\n"
                "Можешь заходить в закрытый канал и получать сигналы 📈"
            )
            await message.answer(txt, parse_mode="HTML")

    text = (
        "👋 <b>Добро пожаловать в TradeX Partner Bot!</b>\n\n"
        "Здесь ты можешь:\n"
        "• получать премиальные крипто-сигналы;\n"
        "• обучаться трейдингу шаг за шагом;\n"
        "• зарабатывать на рынке с продуманным подходом.\n\n"
        "Выбирай действие ниже 👇"
    )
    await message.answer(text, reply_markup=main_keyboard(), parse_mode="HTML")


@dp.message_handler(Text(equals="📌 О боте"))
async def about(message: types.Message):
    text = (
        "🤖 <b>TradeX Partner Bot</b>\n\n"
        "Это твой центр по криптосигналам и обучению.\n\n"
        "🔸 Премиальные сигналы по основным монетам\n"
        "🔸 Чёткая система риск-менеджмента\n"
        "🔸 Обучение трейдингу из 6 модулей\n\n"
        "Начни с раздела <b>«🎓 Обучение трейдингу»</b> или сразу переходи к подписке на сигналы."
    )
    await message.answer(text, parse_mode="HTML")


@dp.message_handler(Text(equals="💰 Тарифы"))
async def tariffs(message: types.Message):
    text = (
        "💰 <b>Тарифы:</b>\n\n"
        f"📅 1 месяц — <b>{PRICE_USDT} USDT</b>\n\n"
        "Оплата в USDT (TRC-20).\n"
        "После оплаты бот автоматически выдаст доступ в закрытый канал."
    )
    await message.answer(text, parse_mode="HTML")


@dp.message_handler(Text(equals="📞 Поддержка"))
async def support(message: types.Message):
    text = (
        "📞 <b>Поддержка</b>\n\n"
        "Если возникли вопросы по оплате или доступу — напиши админу:\n"
        "<code>@your_support_username</code>\n\n"
        "Укажи свой ID (из профиля в боте) и проблему — поможем."
    )
    await message.answer(text, parse_mode="HTML")


@dp.message_handler(Text(equals="👤 Профиль"))
async def profile(message: types.Message):
    row = get_subscription(message.from_user.id)
    now = datetime.now()

    if not row:
        return await message.answer(
            "У тебя пока нет активной подписки.\nНажми «📈 Получить сигналы», чтобы оформить.",
        )

    user_id, unique_price, paid, start_date, end_date, tx_amount, tx_time = row

    try:
        end_dt = datetime.strptime(end_date, "%Y-%m-%d %H:%M")
    except Exception:
        end_dt = now

    status = "🟢 Активна" if paid == 1 and end_dt > now else "🔴 Не активна"
    days_left = max((end_dt - now).days, 0)

    text = (
        "👤 <b>Твой профиль:</b>\n\n"
        f"ID: <code>{user_id}</code>\n"
        f"Статус: {status}\n"
        f"Начало: {start_date}\n"
        f"Окончание: {end_date}\n"
        f"Осталось дней: {days_left}\n"
        f"Последний платёж: {tx_amount} USDT\n"
        f"Время платежа: {tx_time}\n"
    )
    await message.answer(text, parse_mode="HTML")


# ==========================
# ОПЛАТА / УНИКАЛЬНАЯ СУММА
# ==========================

@dp.message_handler(Text(equals="📈 Получить сигналы"))
async def get_signals(message: types.Message):
    unique_tail = random.randint(1, 999)
    unique_price = float(f"{PRICE_USDT}.{unique_tail:03d}")
    user_unique_price[message.from_user.id] = unique_price

    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔄 Проверить оплату")],
            [KeyboardButton(text="⬅️ В главное меню")],
        ],
        resize_keyboard=True,
    )

    text = (
        "🚀 <b>Оплата подписки</b>\n\n"
        f"1️⃣ Отправь <b>РОВНО</b> <code>{unique_price}</code> USDT (TRC-20)\n"
        f"2️⃣ На адрес кошелька:\n<code>{WALLET_ADDRESS}</code>\n\n"
        "⚠️ Сумма должна совпасть до последнего знака, это нужно для автоматической идентификации платежа.\n\n"
        "После отправки нажми «🔄 Проверить оплату»."
    )
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@dp.message_handler(Text(equals="🔄 Проверить оплату"))
async def check_payment_button(message: types.Message):
    await message.answer("⏳ Проверяю оплату, подожди 5–15 секунд...")

    if await check_trx_payment(message.from_user.id):
        amount = user_unique_price.get(message.from_user.id)
        if amount is None:
            return await message.answer("Платёж найден, но уникальная сумма не найдена. Напиши админу.")

        save_payment(message.from_user.id, amount, amount)
        user_unique_price.pop(message.from_user.id, None)

        await message.answer("✅ Платёж подтверждён! Выдаю доступ в канал...")

        try:
            invite = await bot.create_chat_invite_link(CHANNEL_ID, member_limit=1)
            await message.answer(f"🔗 Твоя ссылка в приватный канал:\n{invite.invite_link}")
            await log_to_admin(f"Новая подписка: {message.from_user.id} — {amount} USDT")
        except Exception as e:
            await message.answer(
                "Оплата прошла, но не удалось создать ссылку автоматически.\n"
                "Напиши админу, он выдаст доступ вручную."
            )
            await log_to_admin(f"Ошибка создания ссылки для {message.from_user.id}: {e}")
    else:
        await message.answer(
            "❌ Платёж пока не найден.\n"
            "Если ты только что отправил USDT — подожди 1–2 минуты и нажми ещё раз.\n"
            "Если проблема не пропадает — напиши в поддержку."
        )


@dp.message_handler(Text(equals="⬅️ В главное меню"))
async def back_to_menu(message: types.Message):
    await message.answer("🏠 Главное меню:", reply_markup=main_keyboard())


# ==========================
# АДМИН-ПАНЕЛЬ
# ==========================

@dp.message_handler(Command("admin"))
async def admin_panel(message: types.Message):
    if not is_admin(message.from_user.id):
        return await message.answer("🚫 У тебя нет доступа.")
    await message.answer("👨‍💻 Админ-панель", reply_markup=admin_keyboard())


@dp.message_handler(Text(equals="👥 Все пользователи"))
async def admin_all_users(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    cursor.execute("SELECT user_id, username, first_seen, last_active FROM users")
    rows = cursor.fetchall()

    if not rows:
        return await message.answer("Пока ни один пользователь не открывал бота.")

    text = "👥 <b>Все пользователи:</b>\n\n"
    chunks = []

    for user_id, username, first_seen, last_active in rows:
        text += (
            f"🧑 ID: <code>{user_id}</code>\n"
            f"🔗 Username: @{username if username else 'нет'}\n"
            f"📅 Впервые: {first_seen}\n"
            f"⏱ Активность: {last_active}\n"
            "─────────────────────\n"
        )
        if len(text) > 3500:
            chunks.append(text)
            text = ""

    if text:
        chunks.append(text)

    for chunk in chunks:
        await message.answer(chunk, parse_mode="HTML")


@dp.message_handler(Text(equals="📊 Все подписчики"))
async def admin_all_subs(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    cursor.execute(
        """
        SELECT user_id, unique_price, paid, start_date, end_date, tx_amount, tx_time
        FROM subscriptions
        """
    )
    rows = cursor.fetchall()

    if not rows:
        return await message.answer("Пока нет подписчиков.")

    text = "📄 <b>Список подписчиков:</b>\n\n"
    chunks = []

    for r in rows:
        user_id, unique_price, paid, start_date, end_date, tx_amount, tx_time = r
        status = "🟢 Активна" if paid == 1 else "🔴 Не активна"
        text += (
            f"👤 ID: <code>{user_id}</code>\n"
            f"💵 Уникальная цена: {unique_price}\n"
            f"💰 Оплачено: {tx_amount} USDT\n"
            f"📅 Старт: {start_date}\n"
            f"⏳ Конец: {end_date}\n"
            f"📌 Статус: {status}\n"
            f"⏱ Время платежа: {tx_time}\n"
            "─────────────────────\n"
        )
        if len(text) > 3500:
            chunks.append(text)
            text = ""

    if text:
        chunks.append(text)

    for chunk in chunks:
        await message.answer(chunk, parse_mode="HTML")


@dp.message_handler(Text(equals="🔥 Активные подписчики"))
async def admin_active_subs(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    now = datetime.now()
    cursor.execute(
        """
        SELECT user_id, start_date, end_date, tx_amount
        FROM subscriptions
        WHERE paid = 1
        """
    )
    rows = cursor.fetchall()

    if not rows:
        return await message.answer("Нет активных подписок.")

    text = "🔥 <b>Активные подписчики:</b>\n\n"
    chunks = []

    for user_id, start_date, end_date, tx_amount in rows:
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d %H:%M")
        except Exception:
            end_dt = now

        status = "🟢 АКТИВНА" if end_dt > now else "🔴 ИСТЕКЛА"

        text += (
            f"👤 ID: <code>{user_id}</code>\n"
            f"📅 С: {start_date}\n"
            f"📅 По: {end_date}\n"
            f"💰 Сумма: {tx_amount} USDT\n"
            f"📌 Статус: {status}\n"
            "─────────────────────\n"
        )

        if len(text) > 3500:
            chunks.append(text)
            text = ""

    if text:
        chunks.append(text)

    for chunk in chunks:
        await message.answer(chunk, parse_mode="HTML")


@dp.message_handler(Text(equals="⏳ Истёкшие"))
async def admin_expired_subs(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    now = datetime.now()
    cursor.execute(
        """
        SELECT user_id, start_date, end_date, tx_amount
        FROM subscriptions
        """
    )
    rows = cursor.fetchall()

    expired = []
    for user_id, start_date, end_date, tx_amount in rows:
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d %H:%M")
        except Exception:
            continue
        if end_dt < now:
            expired.append((user_id, start_date, end_date, tx_amount))

    if not expired:
        return await message.answer("Истёкших подписок нет.")

    text = "⏳ <b>Истёкшие подписки:</b>\n\n"
    chunks = []

    for user_id, start_date, end_date, tx_amount in expired:
        text += (
            f"👤 ID: <code>{user_id}</code>\n"
            f"📅 Старт: {start_date}\n"
            f"⏳ Истекла: {end_date}\n"
            f"💰 Оплата: {tx_amount} USDT\n"
            "─────────────────────\n"
        )
        if len(text) > 3500:
            chunks.append(text)
            text = ""

    if text:
        chunks.append(text)

    for chunk in chunks:
        await message.answer(chunk, parse_mode="HTML")


@dp.message_handler(Text(equals="🧾 История платежей"))
async def admin_pay_history(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    cursor.execute(
        """
        SELECT user_id, tx_amount, tx_time
        FROM subscriptions
        WHERE tx_amount IS NOT NULL
        ORDER BY tx_time DESC
        """
    )
    rows = cursor.fetchall()

    if not rows:
        return await message.answer("История платежей пуста.")

    text = "🧾 <b>История платежей:</b>\n\n"
    chunks = []

    for user_id, tx_amount, tx_time in rows:
        text += (
            f"👤 ID: <code>{user_id}</code>\n"
            f"💰 {tx_amount} USDT\n"
            f"⏱ {tx_time}\n"
            "─────────────────────\n"
        )
        if len(text) > 3500:
            chunks.append(text)
            text = ""

    if text:
        chunks.append(text)

    for chunk in chunks:
        await message.answer(chunk, parse_mode="HTML")


@dp.message_handler(Text(equals="📤 Экспорт CSV"))
async def admin_export_csv(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    cursor.execute(
        """
        SELECT user_id, unique_price, paid, start_date, end_date, tx_amount, tx_time
        FROM subscriptions
        """
    )
    rows = cursor.fetchall()

    if not rows:
        return await message.answer("Нет данных для экспорта.")

    filename = "subscriptions_export.csv"
    import csv

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["user_id", "unique_price", "paid", "start_date", "end_date", "tx_amount", "tx_time"])
        for row in rows:
            writer.writerow(row)

    doc = FSInputFile(filename)
    await message.answer_document(doc, caption="Экспорт подписчиков.")


# ==========================
# ФОНОВЫЕ ЗАДАЧИ
# ==========================

async def periodic_expire_check():
    await asyncio.sleep(10)
    while True:
        now = datetime.now()
        cursor.execute(
            """
            SELECT user_id, paid, start_date, end_date, tx_amount, tx_time
            FROM subscriptions
            WHERE paid = 1
            """
        )
        rows = cursor.fetchall()

        for user_id, paid, start_date, end_date, tx_amount, tx_time in rows:
            try:
                end_dt = datetime.strptime(end_date, "%Y-%m-%d %H:%M")
            except Exception:
                continue

            if end_dt < now:
                set_paid(user_id, 0)
                try:
                    await bot.ban_chat_member(CHANNEL_ID, user_id)
                    await bot.unban_chat_member(CHANNEL_ID, user_id)
                except Exception:
                    pass

                try:
                    await bot.send_message(
                        user_id,
                        "⚠️ Твоя подписка истекла. Для продления — оформи оплату снова в боте.",
                    )
                except Exception:
                    pass

                await log_to_admin(f"EXPIRE: подписка {user_id} истекла.")

        await asyncio.sleep(EXPIRE_CHECK_INTERVAL)


async def periodic_auto_check_payments():
    await asyncio.sleep(15)
    while True:
        if user_unique_price:
            for user_id in list(user_unique_price.keys()):
                try:
                    if await check_trx_payment(user_id):
                        amount = user_unique_price.get(user_id)
                        if amount is None:
                            continue
                        save_payment(user_id, amount, amount)
                        user_unique_price.pop(user_id, None)

                        try:
                            invite = await bot.create_chat_invite_link(CHANNEL_ID, member_limit=1)
                            await bot.send_message(
                                user_id,
                                f"✅ Оплата найдена автоматически!\nВот ссылка в канал:\n{invite.invite_link}",
                            )
                        except Exception as e:
                            await bot.send_message(
                                user_id,
                                "Оплата прошла, но не удалось создать ссылку автоматически.\n"
                                "Напиши админу, он выдаст доступ.",
                            )
                            await log_to_admin(f"AUTO-LINK ERROR {user_id}: {e}")

                        await log_to_admin(f"AUTO-PAYMENT: {user_id} — {amount} USDT")
                except Exception as e:
                    logging.error(f"Ошибка в periodic_auto_check_payments: {e}")

        await asyncio.sleep(PAYMENT_SCAN_INTERVAL)


# ==========================
# ЗАПУСК
# ==========================

async def on_startup(dp: Dispatcher):
    asyncio.create_task(periodic_expire_check())
    asyncio.create_task(periodic_auto_check_payments())
    logging.info("Бот запущен.")


if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
