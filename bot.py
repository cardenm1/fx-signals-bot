import asyncio
from datetime import datetime, timedelta, timezone

import dukascopy_python as duka
from dukascopy_python.instruments import (
    INSTRUMENT_FX_MAJORS_GBP_USD,
)

PIP = 0.0001
MAX_DATA_AGE_MINUTES = 20


def get_market_data():
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=3)

    print("GBP/USD M5 MONITOR")
    print("Current UTC:", now.isoformat())

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
        print("No market data available")
        return None

    common = bid.index.intersection(ask.index)

    completed = [
        t for t in common
        if t.to_pydatetime() + timedelta(minutes=5) <= now
    ]

    if not completed:
        print("No completed M5 candles")
        return None

    candle_time = max(completed)
    age = (
        now - candle_time.to_pydatetime()
    ).total_seconds() / 60

    if age > MAX_DATA_AGE_MINUTES:
        print("Market data is stale")
        return None

    bid_close = float(bid.loc[candle_time, "close"])
    ask_close = float(ask.loc[candle_time, "close"])

    spread = (ask_close - bid_close) / PIP

    if spread < 0:
        raise RuntimeError("Negative spread detected")

    print("Last completed M5:", candle_time)
    print("BID:", round(bid_close, 5))
    print("ASK:", round(ask_close, 5))
    print("Spread:", round(spread, 2), "pips")
    print("Market data check successful")

    return {
        "time": candle_time,
        "bid": bid_close,
        "ask": ask_close,
        "spread": spread,
    }


async def main():
    data = get_market_data()

    if data is None:
        print("No valid fresh candle")
        return

    print("Strategy integration pending")
    print("No Telegram notification sent")


if __name__ == "__main__":
    asyncio.run(main())
