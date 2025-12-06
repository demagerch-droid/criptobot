# auto_signals.py

import asyncio
import random
import logging
from decimal import Decimal
from typing import Optional, Sequence

import aiohttp
from aiogram import Bot

logger = logging.getLogger(__name__)

# Базовый URL CoinGecko
COINGECKO_API_BASE = "https://api.coingecko.com/api/v3"

# Маппинг наших пар на ID в CoinGecko
COINGECKO_IDS = {
    "BTCUSDT": "bitcoin",
    "ETHUSDT": "ethereum",
    "SOLUSDT": "solana",
    "BNBUSDT": "binancecoin",
    # если добавишь пары в AUTO_SIGNALS_SYMBOLS – не забудь дописать сюда
}


async def fetch_coingecko_price(coin_id: str) -> Optional[dict]:
    """
    Берём цену и 24h изменение по монете с CoinGecko.
    Используем /simple/price с vs_currencies=usd и include_24hr_change=true.
    """
    url = f"{COINGECKO_API_BASE}/simple/price"
    params = {
        "ids": coin_id,
        "vs_currencies": "usd",
        "include_24hr_change": "true",
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, params=params, timeout=10) as resp:
                if resp.status != 200:
                    logger.warning("CoinGecko price %s status %s", coin_id, resp.status)
                    return None
                data = await resp.json()
                return data
        except Exception as e:
            logger.error("Error fetching CoinGecko price for %s: %s", coin_id, e)
            return None


def _format_price(p: Decimal) -> str:
    """
    Примитивное форматирование: чем меньше цена, тем больше знаков.
    """
    if p >= Decimal("100"):
        q = p.quantize(Decimal("0.1"))
    elif p >= Decimal("1"):
        q = p.quantize(Decimal("0.01"))
    elif p >= Decimal("0.1"):
        q = p.quantize(Decimal("0.001"))
    else:
        q = p.quantize(Decimal("0.0001"))
    return str(q)


async def build_auto_signal_text(
    symbols: Sequence[str],
    enabled: bool,
) -> Optional[str]:
    """
    Улучшенная версия авто-сигнала:
    • берём пару из списка symbols
    • тянем цену и 24h изменение с CoinGecko
    • фильтруем слишком слабое и слишком дикое движение
    • для BTC/ETH даём более мягкие SL/TP, для альтов — агрессивнее
    • вход показываем диапазоном
    """
    if not enabled:
        return None

    symbols = list(symbols) or ["BTCUSDT"]
    pair = random.choice(symbols)

    coin_id = COINGECKO_IDS.get(pair)
    if not coin_id:
        logger.warning("No CoinGecko ID for pair %s", pair)
        return None

    data = await fetch_coingecko_price(coin_id)
    if not data or coin_id not in data:
        return None

    coin_data = data[coin_id]
    price_usd = coin_data.get("usd")
    change_percent = coin_data.get("usd_24h_change")

    try:
        price = Decimal(str(price_usd))
    except Exception:
        return None

    try:
        chg = Decimal(str(change_percent)) if change_percent is not None else None
    except Exception:
        chg = None

    # Если не смогли посчитать изменение — просто выходим
    if chg is None:
        return None

    # Фильтр по движению: слишком слабое и слишком дикое движение пропускаем
    abs_chg = chg.copy_abs()
    if abs_chg < Decimal("1.5"):
        # меньше 1.5% за сутки — флет, сигнал не даём
        return None
    if abs_chg > Decimal("18"):
        # больше 18% за сутки — слишком агрессивный памп/дамп, тоже пропускаем
        return None

    # Определяем направление
    if chg > Decimal("1"):
        direction = "long"
        idea = "🟢 Идея: LONG (преобладает восходящее движение за 24ч)"
    elif chg < Decimal("-1"):
        direction = "short"
        idea = "🔴 Идея: SHORT (преобладает нисходящее движение за 24ч)"
    else:
        # сюда в теории не попадём из-за фильтра, но пусть будет
        direction = None
        idea = "⚪ Рынок во флете, явного тренда за 24ч нет. Сигнал без конкретных уровней."

    # Если по какой-то причине направления нет — просто обзор без уровней
    if direction is None:
        parts = [
            f"📡 <b>Авто-сигнал</b> по <b>{pair}</b>",
            f"Текущая цена: <b>{_format_price(price)}</b> USDT",
            f"Изменение за 24ч: <b>{chg}%</b>",
            "",
            idea,
            "",
            "⚠️ Это автоматический технический сигнал от бота, не финансовая рекомендация.",
        ]
        return "\n".join(parts)

    # Разные проценты для BTC/ETH и альтов
    if pair in ("BTCUSDT", "ETHUSDT"):
        sl_pct = Decimal("0.005")   # 0.5%
        tp1_pct = Decimal("0.01")   # 1%
        tp2_pct = Decimal("0.02")   # 2%
    else:
        sl_pct = Decimal("0.01")    # 1%
        tp1_pct = Decimal("0.02")   # 2%
        tp2_pct = Decimal("0.04")   # 4%

    entry_mid = price

    # Вход диапазоном
    if direction == "long":
        entry_low = entry_mid * (Decimal("1") - Decimal("0.002"))   # -0.2%
        entry_high = entry_mid
        sl = entry_mid * (Decimal("1") - sl_pct)
        tp1 = entry_mid * (Decimal("1") + tp1_pct)
        tp2 = entry_mid * (Decimal("1") + tp2_pct)
        dir_text = "LONG"
    else:  # short
        entry_low = entry_mid
        entry_high = entry_mid * (Decimal("1") + Decimal("0.002"))  # +0.2%
        sl = entry_mid * (Decimal("1") + sl_pct)
        tp1 = entry_mid * (Decimal("1") - tp1_pct)
        tp2 = entry_mid * (Decimal("1") - tp2_pct)
        dir_text = "SHORT"

    parts = [
        f"📡 <b>Авто-сигнал</b> по <b>{pair}</b>",
        f"Текущая цена: <b>{_format_price(price)}</b> USDT",
        f"Изменение за 24ч: <b>{chg}%</b>",
        "",
        idea,
        "",
        f"📊 <b>Параметры сделки ({dir_text})</b>",
        f"Вход: <b>{_format_price(entry_low)}</b>–<b>{_format_price(entry_high)}</b> USDT",
        f"Стоп-лосс: <b>{_format_price(sl)}</b> USDT",
        f"Тейк-профит 1: <b>{_format_price(tp1)}</b> USDT",
        f"Тейк-профит 2: <b>{_format_price(tp2)}</b> USDT",
        "",
        "⚠️ Это автоматический технический сигнал от бота, не финансовая рекомендация.",
        "Риск-менеджмент: не рискуй более 1–2% депозита на сделку и всегда используй стоп-лосс.",
    ]

    return "\n".join(parts)


    # Считаем вход / стоп / тейки (простая модель по % от цены)
    entry = price

    if direction == "long":
        sl = entry * (Decimal("1") - Decimal("0.01"))   # -1%
        tp1 = entry * (Decimal("1") + Decimal("0.02"))  # +2%
        tp2 = entry * (Decimal("1") + Decimal("0.04"))  # +4%
        dir_text = "LONG"
    else:  # short
        sl = entry * (Decimal("1") + Decimal("0.01"))   # +1%
        tp1 = entry * (Decimal("1") - Decimal("0.02"))  # -2%
        tp2 = entry * (Decimal("1") - Decimal("0.04"))  # -4%
        dir_text = "SHORT"

    parts = [
        f"📡 <b>Авто-сигнал</b> по <b>{pair}</b>",
        f"Текущая цена: <b>{_format_price(price)}</b> USDT",
    ]
    if chg is not None:
        parts.append(f"Изменение за 24ч: <b>{chg}%</b>")
    if idea:
        parts.append("")
        parts.append(idea)

    parts.append("")
    parts.append(f"📊 <b>Параметры сделки ({dir_text})</b>")
    parts.append(f"Вход: <b>{_format_price(entry)}</b> USDT")
    parts.append(f"Стоп-лосс: <b>{_format_price(sl)}</b> USDT")
    parts.append(f"Тейк-профит 1: <b>{_format_price(tp1)}</b> USDT")
    parts.append(f"Тейк-профит 2: <b>{_format_price(tp2)}</b> USDT")

    parts.append("")
    parts.append("⚠️ Это автоматический технический сигнал от бота, не финансовая рекомендация.")

    return "\n".join(parts)


async def auto_signals_worker(
    bot: Bot,
    signals_channel_id: int,
    auto_signals_per_day: int,
    symbols: Sequence[str],
    enabled: bool,
) -> None:
    """
    Фоновая задача: раз в N секунд шлёт авто-сигнал в канал.
    """
    if not enabled:
        logger.info("Auto signals disabled, worker not started.")
        return

    if not isinstance(signals_channel_id, int):
        logger.warning("signals_channel_id is not int, auto-signals disabled.")
        return

    interval = int(24 * 3600 / max(auto_signals_per_day, 1))

    # немного ждём старт бота
    await asyncio.sleep(15)

    while True:
        try:
            text = await build_auto_signal_text(symbols, enabled)
            if text:
                await bot.send_message(signals_channel_id, text)
                logger.info("Auto signal sent to %s", signals_channel_id)
        except Exception as e:
            logger.error("Auto signals worker error: %s", e)

        await asyncio.sleep(interval)
