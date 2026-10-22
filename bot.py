
import asyncio
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Bot

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


async def main():
    now = datetime.now(
        ZoneInfo("Europe/London")
    ).strftime("%d/%m/%Y %H:%M")

    print(f"GBP/USD monitor started: {now}")

    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Telegram configuration missing")

    # Market data and strategy will be added next.
    # Do not generate signals without verified data.
    signal = None

    if signal is None:
        print("No signal. Telegram message skipped.")
        return

    async with Bot(token=TOKEN) as bot:
        await bot.send_message(
            chat_id=CHAT_ID,
            text=signal
        )


if __name__ == "__main__":
    asyncio.run(main())
