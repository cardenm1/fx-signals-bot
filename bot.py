
import os
import asyncio
from datetime import datetime, timedelta, timezone

import dukascopy_python as duka
from dukascopy_python.instruments import (
    INSTRUMENT_FX_MAJORS_GBP_USD
)
from telegram import Bot

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

PIP = 0.0001


def get_market_data():
    now = datetime.now(timezone.utc)

    # Fetch recent BID and ASK candles
    start = now - timedelta(hours=3)

    bid = duka.fetch(
        instrument=INSTRUMENT_FX_MAJORS_GBP_USD,
        interval=duka.INTERVAL_MIN_5,
        offer_side=duka.OFFER_SIDE_BID,
        start=start,
        end=now,
    )

    ask = duka.fetch(
        instrument=INSTRUMENT_FX_MAJORS_GBP_USD,
        interval=duka.INTERVAL_MIN_5,
        offer_side=duka.OFFER_SIDE_ASK,
        start=start,
        end=now,
    )

    if bid.empty or ask.empty:
        raise RuntimeError("No market data")

    common = bid.index.intersection(ask.index)

    # Only completed 5-minute candles
    completed = [
        t for t in common
        if t.to_pydatetime() + timedelta(minutes=5) <= now
    ]

    if not completed:
        raise RuntimeError("No completed M5 candles")

    candle_time = max(completed)

    # Avoid stale data, including weekends
    age = (now - candle_time.to_pydatetime()).total_seconds()

    if age > 20 * 60:
        print("Market data stale. Skipping.")
        return None

    bid_close = float(bid.loc[candle_time, "close"])
    ask_close = float(ask.loc[candle_time, "close"])

    spread = (ask_close - bid_close) / PIP

    if spread < 0:
        raise RuntimeError("Negative spread")

    return {
        "time": candle_time,
        "bid": bid_close,
        "ask": ask_close,
        "spread": spread,
    }


async def main():
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Telegram secrets missing")

    data = get_market_data()

    if data is None:
        return

    candle_time = data["time"].strftime(
        "%d/%m/%Y %H:%M UTC"
    )

    message = (
        "GBP/USD LIVE MARKET\n\n"
        f"M5 candle: {candle_time}\n"
        f"BID: {data['bid']:.5f}\n"
        f"ASK: {data['ask']:.5f}\n"
        f"Spread: {data['spread']:.2f} pips\n\n"
        "Mode: PAPER TRADING\n"
        "Status: Market data only\n"
        "BUY/SELL signals: Not enabled"
    )

    async with Bot(token=TOKEN) as bot:
        await bot.send_message(
            chat_id=CHAT_ID,
            text=message,
        )

    print("Market update sent to Telegram")


if __name__ == "__main__":
    asyncio.run(main())
