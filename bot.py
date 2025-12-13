# auto_signals.py

import asyncio
import random
import logging
from decimal import Decimal
from typing import Optional, Sequence, List, Tuple
from datetime import datetime

import aiohttp
from aiogram import Bot

logger = logging.getLogger(__name__)

# --- ТИХИЕ ЧАСЫ (по твоему локальному времени) ---

QUIET_HOURS_ENABLED = True   # если хочешь сигналы 24/7 — поставь False
QUIET_HOURS_START = 0        # c 00:00
QUIET_HOURS_END = 7          # до 07:00 сигналы не шлём
QUIET_HOURS_UTC_OFFSET = 2   # сдвиг от UTC (Киев зимой +2, летом можешь поставить 3)

# --- CoinGecko --- 

COINGECKO_API_BASE = "https://api.coingecko.com/api/v3"

# Маппинг наших пар на CoinGecko ID
COINGECKO_IDS = {
    "BTCUSDT": "bitcoin",
    "ETHUSDT": "ethereum",
    "SOLUSDT": "solana",
    "BNBUSDT": "binancecoin",
    # если добавишь пары в AUTO_SIGNALS_SYMBOLS — не забудь дописать сюда
}

# Параметры «стратегии»
FAST_EMA_PERIOD = 20      # быстрая EMA по закрытиям
SLOW_EMA_PERIOD = 50      # медленная EMA (фильтр тренда)
ATR_PERIOD = 14           # сколько последних интервалов для волатильности

# Фильтры по тренду и волатильности
MIN_TREND_PCT = Decimal("0.3")  # минимальная сила тренда относительно EMA50 (в %)
MIN_ATR_PCT = Decimal("0.2")    # слишком низкая вола (менее 0.2% за свечу) — не торгуем
MAX_ATR_PCT = Decimal("6")      # слишком бешеная вола (более 6% за свечу) — тоже не лезем


# ---------- ЗАГРУЗКА СВЕЧ ИЗ COINGECKO (через market_chart) ----------

async def fetch_coingecko_market_chart(coin_id: str, days: int = 3) -> Optional[List[Tuple[int, Decimal]]]:
    """
    Берём исторический график с CoinGecko:
    /coins/{id}/market_chart?vs_currency=usd&days=3

    Для 1–90 дней CoinGecko на бесплатном плане даёт данные с часовым шагом —
    нам этого достаточно, чтобы посчитать EMA и волатильность по закрытиям.
    """
    url = f"{COINGECKO_API_BASE}/coins/{coin_id}/market_chart"
    params = {
        "vs_currency": "usd",
        "days": days,
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, params=params, timeout=10) as resp:
                if resp.status != 200:
                    logger.warning("CoinGecko market_chart %s status %s", coin_id, resp.status)
                    return None
                data = await resp.json()
        except Exception as e:
            logger.error("Error fetching CoinGecko market_chart for %s: %s", coin_id, e)
            return None

    prices = data.get("prices")
    if not prices or len(prices) < 10:
        return None

    series: List[Tuple[int, Decimal]] = []
    for ts, price in prices:
        try:
            ts_int = int(ts)
            p_dec = Decimal(str(price))
        except Exception:
            continue
        series.append((ts_int, p_dec))

    if len(series) < 10:
        return None

    return series


# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------

def _format_price(p: Decimal) -> str:
    """Формат цены с разумным количеством знаков."""
    if p >= Decimal("100"):
        q = p.quantize(Decimal("0.1"))
    elif p >= Decimal("1"):
        q = p.quantize(Decimal("0.01"))
    elif p >= Decimal("0.1"):
        q = p.quantize(Decimal("0.001"))
    else:
        q = p.quantize(Decimal("0.0001"))
    return str(q)


def _format_pct(x: Decimal) -> str:
    """Формат процента с 2 знаками."""
    q = x.quantize(Decimal("0.01"))
    return str(q)


def _ema(values: Sequence[Decimal], period: int) -> Optional[Decimal]:
    """Классическая EMA по списку значений."""
    if len(values) < period:
        return None
    alpha = Decimal("2") / Decimal(period + 1)
    ema_val = values[0]
    for v in values[1:]:
        ema_val = (v - ema_val) * alpha + ema_val
    return ema_val


def _atr_like(values: Sequence[Decimal], period: int) -> Optional[Decimal]:
    """
    Простейший ATR-подобный показатель:
    среднее абсолютное изменение между соседними закрытиями за N последних интервалов.
    Не классический ATR, но даёт адекватную оценку волатильности.
    """
    if len(values) <= period:
        return None
    diffs = []
    for i in range(-period, 0):
        try:
            prev_v = values[i - 1]
            cur_v = values[i]
        except IndexError:
            continue
        diffs.append(abs(cur_v - prev_v))
    if not diffs:
        return None
    return sum(diffs, Decimal("0")) / Decimal(len(diffs))


# ---------- ПОСТРОЕНИЕ СИГНАЛА ПО СВЕЧАМ + EMA + ВОЛАТИЛЬНОСТИ ----------


async def build_auto_signal_text(
    symbols: Sequence[str],
    enabled: bool,
) -> Optional[str]:
    """
    Генерация авто-сигнала на основе:
    • исторических данных с CoinGecko (серия закрытий ~1H)
    • EMA20 / EMA50 (фильтр тренда)
    • ATR-подобной волатильности (по закрытиям)
    • уровней Fibonacci (retracement 0.5–0.618, targets 1.272/1.618)

    Важно: это не «гарантия профита», а форматирование сигналов по понятной системе.
    """
    if not enabled:
        return None

    symbols = list(symbols) or ["BTCUSDT"]
    pair = random.choice(symbols)

    coin_id = COINGECKO_IDS.get(pair)
    if not coin_id:
        logger.warning("No CoinGecko ID for pair %s", pair)
        return None

    # Берём ~3 дня истории (на бесплатном плане CoinGecko обычно отдаёт почасовые точки)
    series = await fetch_coingecko_market_chart(coin_id, days=3)
    if not series:
        return None

    closes = [p for _, p in series]
    if len(closes) < max(SLOW_EMA_PERIOD, ATR_PERIOD) + 10:
        return None

    last_close = closes[-1]

    ema_fast = _ema(closes, FAST_EMA_PERIOD)
    ema_slow = _ema(closes, SLOW_EMA_PERIOD)
    if ema_fast is None or ema_slow is None:
        return None

    atr = _atr_like(closes, ATR_PERIOD)
    if atr is None or atr <= 0:
        return None

    # Сила тренда относительно EMA50
    trend_pct = (last_close - ema_slow) / last_close * Decimal("100")
    atr_pct = atr / last_close * Decimal("100")

    # Фильтр по волатильности
    if atr_pct < MIN_ATR_PCT or atr_pct > MAX_ATR_PCT:
        return None

    # Направление по тренду
    direction = None
    idea_line = None
    if trend_pct > MIN_TREND_PCT and ema_fast > ema_slow:
        direction = "long"
        idea_line = "🟢 Идея: <b>LONG по тренду</b> (EMA20 выше EMA50)."
    elif trend_pct < -MIN_TREND_PCT and ema_fast < ema_slow:
        direction = "short"
        idea_line = "🔴 Идея: <b>SHORT по тренду</b> (EMA20 ниже EMA50)."
    else:
        return None

    # --- Fibonacci swing (по закрытиям) ---
    SWING_LOOKBACK = 60  # ~60 часов
    lookback = min(SWING_LOOKBACK, len(closes))
    window = closes[-lookback:]

    # Импульс для фибо: LONG -> low→high, SHORT -> high→low
    if direction == "long":
        swing_low = min(window)
        low_i = window.index(swing_low)
        swing_high = max(window[low_i:]) if low_i < len(window) else max(window)
    else:
        swing_high = max(window)
        high_i = window.index(swing_high)
        swing_low = min(window[high_i:]) if high_i < len(window) else min(window)

    if swing_high <= swing_low:
        return None

    swing_range = swing_high - swing_low

    # чтобы не строить фибо на «пустом месте»
    MIN_SWING_ATR_MULT = Decimal("2")
    if swing_range < atr * MIN_SWING_ATR_MULT:
        return None

    # Уровни retracement
    r382 = Decimal("0.382")
    r50 = Decimal("0.5")
    r618 = Decimal("0.618")
    r786 = Decimal("0.786")

    # Targets extension
    ext1 = Decimal("1.272")
    ext2 = Decimal("1.618")

    # буфер для стопа
    sl_buffer = atr * Decimal("0.25")

    if direction == "long":
        fib_382 = swing_high - swing_range * r382
        fib_50 = swing_high - swing_range * r50
        fib_618 = swing_high - swing_range * r618
        fib_786 = swing_high - swing_range * r786

        entry_low = min(fib_618, fib_50)
        entry_high = max(fib_618, fib_50)
        sl = fib_786 - sl_buffer
        tp1 = swing_high + swing_range * (ext1 - Decimal("1"))
        tp2 = swing_high + swing_range * (ext2 - Decimal("1"))
        dir_text = "LONG"
        swing_text = f"{_format_price(swing_low)} → {_format_price(swing_high)}"
    else:
        fib_382 = swing_low + swing_range * r382
        fib_50 = swing_low + swing_range * r50
        fib_618 = swing_low + swing_range * r618
        fib_786 = swing_low + swing_range * r786

        entry_low = min(fib_50, fib_618)
        entry_high = max(fib_50, fib_618)
        sl = fib_786 + sl_buffer
        tp1 = swing_low - swing_range * (ext1 - Decimal("1"))
        tp2 = swing_low - swing_range * (ext2 - Decimal("1"))
        dir_text = "SHORT"
        swing_text = f"{_format_price(swing_high)} → {_format_price(swing_low)}"

    # Красивый текст сигнала (важно: сохраняем строки Вход/Стоп/TP1/TP2 для парсера бота)
    parts = [
        f"📈 <b>Сигнал</b> по <b>{pair[:-4]}/{pair[-4:]}</b>",
        f"🕒 Таймфрейм: <b>1H</b> (данные CoinGecko)",
        f"💵 Текущая цена: <b>{_format_price(last_close)}</b> USDT",
        f"📉 Волатильность (ATR~): <b>{_format_pct(atr_pct)}%</b> / свеча",
        f"📈 Тренд к EMA{SLOW_EMA_PERIOD}: <b>{_format_pct(trend_pct)}%</b>",
        "",
        idea_line,
        "",
        "🧬 <b>Fibonacci</b>",
        f"• Импульс (swing): <b>{swing_text}</b>",
        "• Зона входа: <b>0.5–0.618</b> (откат)",
        "• Цели: <b>1.272</b> и <b>1.618</b> (extension)",
        "",
        f"📊 <b>Параметры сделки ({dir_text})</b>",
        f"Вход: <b>{_format_price(entry_low)}</b>–<b>{_format_price(entry_high)}</b> USDT",
        f"Стоп-лосс: <b>{_format_price(sl)}</b> USDT",
        f"Тейк-профит 1: <b>{_format_price(tp1)}</b> USDT",
        f"Тейк-профит 2: <b>{_format_price(tp2)}</b> USDT",
        "",
        "🧠 Рекомендация: фиксируй часть на TP1 и переводи сделку в <b>безубыток</b>.",
        "⚠️ Риск-менеджмент: не рискуй более 3–6% депозита на сделку и всегда используй стоп-лосс.",
    ]

    return "\n".join(parts)


# ---------- ВОРКЕР, КОТОРЫЙ РАЗ В N ЧАСОВ ДАЁТ СИГНАЛЫ ---------- 

async def auto_signals_worker(
    bot: Bot,
    signals_channel_id: int,
    auto_signals_per_day: int,
    symbols: Sequence[str],
    enabled: bool,
) -> None:
    """
    Фоновая задача:
    • раз в N часов пробует сгенерировать сигнал
    • учитывает тихие часы
    • если фильтры не проходят — просто ничего не шлёт
    """
    if not enabled:
        logger.info("Auto signals disabled, worker not started.")
        return

    if not isinstance(signals_channel_id, int):
        logger.warning("signals_channel_id is not int, auto-signals disabled.")
        return

    interval = int(24 * 3600 / max(auto_signals_per_day, 1))

    # Немного ждём старт бота
    await asyncio.sleep(15)

    while True:
        try:
            now_utc = datetime.utcnow()
            local_hour = (now_utc.hour + QUIET_HOURS_UTC_OFFSET) % 24

            in_quiet = False
            if QUIET_HOURS_ENABLED:
                if QUIET_HOURS_START <= QUIET_HOURS_END:
                    # обычный диапазон, напр. 0–7
                    in_quiet = QUIET_HOURS_START <= local_hour < QUIET_HOURS_END
                else:
                    # диапазон через полночь, напр. 23–7
                    in_quiet = local_hour >= QUIET_HOURS_START or local_hour < QUIET_HOURS_END

            if in_quiet:
                logger.info("Auto signal skipped due to quiet hours (local hour=%s)", local_hour)
            else:
                text = await build_auto_signal_text(symbols, enabled)
                if text:
                    await bot.send_message(signals_channel_id, text)
                    logger.info("Auto signal sent to %s", signals_channel_id)
        except Exception as e:
            logger.error("Auto signals worker error: %s", e)

        await asyncio.sleep(interval)
