
import os
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Bot

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

async def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

    if not CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID is missing")

    now = datetime.now(
        ZoneInfo("Europe/London")
    ).strftime("%d/%m/%Y %H:%M")

    message = (
        "GBP/USD SIGNAL BUDDY\n\n"
        "Telegram connection: OK\n"
        f"UK time: {now}\n\n"
        "Mode: PAPER TRADING\n"
        "Strategy: H1 + Fibonacci 50% + M5 BOS\n\n"
        "This is a connection test.\n"
        "Market signals are not active yet."
    )

    async with Bot(token=TOKEN) as bot:
        await bot.send_message(
            chat_id=CHAT_ID,
            text=message
        )

    print("Telegram test message sent successfully")

if __name__ == "__main__":
    asyncio.run(main())
