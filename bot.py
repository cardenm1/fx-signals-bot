
import asyncio
import os
from datetime import datetime, timezone

import dukascopy_python
from dukascopy_python.instruments import INSTRUMENT_FX_MAJORS_GBP_USD
from telegram import Bot

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


async def main():
    now = datetime.now(timezone.utc)
    print("Checking Dukascopy GBP/USD data")
    print("UTC:", now.isoformat())

    try:
        data = dukascopy_python.fetch(
            instrument=INSTRUMENT_FX_MAJORS_GBP_USD,
            interval=dukascopy_python.INTERVAL_MIN_5,
            offer_side=dukascopy_python.OFFER_SIDE_BID,
            start=now.replace(hour=0, minute=0, second=0, microsecond=0),
            end=now,
        )

        print("Candles received:", len(data))
        if data.empty:
            print("No candles available")
            return

        print("Latest candles:")
        print(data.tail(3).to_string())

    except Exception as error:
        print("Dukascopy error:", repr(error))
        raise

    print("Data connection test completed")


if __name__ == "__main__":
    asyncio.run(main())
