
import asyncio
from datetime import datetime, timedelta, timezone

import dukascopy_python
from dukascopy_python.instruments import INSTRUMENT_FX_MAJORS_GBP_USD

INSTRUMENT = INSTRUMENT_FX_MAJORS_GBP_USD


def load_candles(side, start, end):
    return dukascopy_python.fetch(
        instrument=INSTRUMENT,
        interval=dukascopy_python.INTERVAL_MIN_5,
        offer_side=side,
        start=start,
        end=end,
    )


async def main():
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=3)

    print("GBP/USD BID/ASK CHECK")
    print("UTC:", end.isoformat())

    bid = load_candles(
        dukascopy_python.OFFER_SIDE_BID,
        start,
        end,
    )

    ask = load_candles(
        dukascopy_python.OFFER_SIDE_ASK,
        start,
        end,
    )

    print("BID candles:", len(bid))
    print("ASK candles:", len(ask))

    if bid.empty or ask.empty:
        raise RuntimeError("Missing BID or ASK data")

    common = bid.index.intersection(ask.index)

    if common.empty:
        raise RuntimeError("BID/ASK timestamps do not match")

    last_time = common.max()
    bid_close = float(bid.loc[last_time, "close"])
    ask_close = float(ask.loc[last_time, "close"])

    spread = (ask_close - bid_close) * 10000

    print("Last candle UTC:", last_time)
    print("BID close:", bid_close)
    print("ASK close:", ask_close)
    print("Spread pips:", round(spread, 2))

    if spread < 0:
        raise RuntimeError("Negative spread detected")

    age_minutes = (
        end - last_time.to_pydatetime()
    ).total_seconds() / 60

    print("Candle age minutes:", round(age_minutes, 1))

    if age_minutes > 20:
        print("WARNING: Market data may be stale")

    print("BID/ASK check completed")


if __name__ == "__main__":
    asyncio.run(main())
