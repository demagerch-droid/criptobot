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
    • исторических данных с CoinGecko (серия закрытий)
    • EMA20 / EMA50 (тренд)
    • ATR-подобной волатильности за 14 интервалов
    • фильтров по тренду и волатильности
    """
    if not enabled:
        return None

    symbols = list(symbols) or ["BTCUSDT"]
    pair = random.choice(symbols)

    coin_id = COINGECKO_IDS.get(pair)
    if not coin_id:
        logger.warning("No CoinGecko ID for pair %s", pair)
        return None

    # Берём ~3 дня истории, там будут почасовые точки
    series = await fetch_coingecko_market_chart(coin_id, days=3)
    if not series:
        return None

    closes = [p for _, p in series]
    if len(closes) < max(SLOW_EMA_PERIOD, ATR_PERIOD) + 5:
        # мало данных, лучше ничего не давать, чем городить мусор
        return None

    last_close = closes[-1]

    ema_fast = _ema(closes, FAST_EMA_PERIOD)
    ema_slow = _ema(closes, SLOW_EMA_PERIOD)
    if ema_fast is None or ema_slow is None:
        return None

    atr = _atr_like(closes, ATR_PERIOD)
    if atr is None or atr <= 0:
        return None

    # Сила тренда относительно медленной EMA
    trend_pct = (last_close - ema_slow) / last_close * Decimal("100")
    # Средняя волатильность в процентах
    atr_pct = atr / last_close * Decimal("100")

    # Фильтр по волатильности
    if atr_pct < MIN_ATR_PCT or atr_pct > MAX_ATR_PCT:
        # либо слишком скучно, либо слишком бешено — пропускаем
        return None

    direction = None
    idea_lines = []

    # Фильтр по тренду: цена + EMA20 + EMA50 должны смотреть в одну сторону
    if trend_pct > MIN_TREND_PCT and ema_fast > ema_slow:
        direction = "long"
        idea_lines.append("🟢 Идея: LONG по тренду (цена выше EMA, бычий наклон).")
    elif trend_pct < -MIN_TREND_PCT and ema_fast < ema_slow:
        direction = "short"
        idea_lines.append("🔴 Идея: SHORT по тренду (цена ниже EMA, медвежий наклон).")
    else:
        # тренд слабый/размазанный — не даём сигнал
        return None

    # Разные настройки для BTC/ETH и альтов
    if pair in ("BTCUSDT", "ETHUSDT"):
        sl_mult = Decimal("1.5")   # стоп ~1.5 ATR
        tp1_mult = Decimal("1.5")  # TP1 ~1.5 ATR
        tp2_mult = Decimal("3")    # TP2 ~3 ATR
    else:
        sl_mult = Decimal("1.8")   # альты агрессивнее
        tp1_mult = Decimal("2")
        tp2_mult = Decimal("4")

    entry_mid = last_close
    entry_zone = atr * Decimal("0.5")  # вход диапазоном ≈ пол-ATR

    if direction == "long":
        entry_low = entry_mid - entry_zone
        entry_high = entry_mid
        sl = entry_mid - sl_mult * atr
        tp1 = entry_mid + tp1_mult * atr
        tp2 = entry_mid + tp2_mult * atr
        dir_text = "LONG"
    else:
        entry_low = entry_mid
        entry_high = entry_mid + entry_zone
        sl = entry_mid + sl_mult * atr
        tp1 = entry_mid - tp1_mult * atr
        tp2 = entry_mid - tp2_mult * atr
        dir_text = "SHORT"

    parts = [
        f"📡 <b>Авто-сигнал (EMA + волатильность)</b> по <b>{pair}</b>",
        f"Текущая цена (закрытие последнего интервала): <b>{_format_price(last_close)}</b> USDT",
        f"Сила тренда относительно EMA{SLOW_EMA_PERIOD}: <b>{_format_pct(trend_pct)}%</b>",
        f"Средняя волатильность за {ATR_PERIOD} интервалов: <b>{_format_pct(atr_pct)}%</b> за свечу",
        "",
    ]
    parts.extend(idea_lines)
    parts.extend(
        [
            "",
            f"📊 <b>Параметры сделки ({dir_text})</b>",
            f"Вход: <b>{_format_price(entry_low)}</b>–<b>{_format_price(entry_high)}</b> USDT",
            f"Стоп-лосс: <b>{_format_price(sl)}</b> USDT",
            f"Тейк-профит 1: <b>{_format_price(tp1)}</b> USDT",
            f"Тейк-профит 2: <b>{_format_price(tp2)}</b> USDT",
            "",
            "⚠️ Это автоматический технический сигнал по свечам и EMA, не финансовая рекомендация.",
            "Риск-менеджмент: не рискуй более 1–2% депозита на сделку и всегда используй стоп-лосс.",
        ]
    )

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
