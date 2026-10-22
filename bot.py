
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import dukascopy_python as duka
from dukascopy_python.instruments import (
    INSTRUMENT_FX_MAJORS_GBP_USD,
)
from telegram import Bot

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

STATE_FILE = Path("signal_state.json")
PIP = 0.0001


def fetch_data():
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=5)

    def fetch(side):
        df = duka.fetch(
            instrument=INSTRUMENT_FX_MAJORS_GBP_USD,
            interval=duka.INTERVAL_MIN_5,
            offer_side=side,
            start=start,
            end=now,
        )
        df.index = pd.to_datetime(df.index, utc=True)
        return df.sort_index()

    bid = fetch(duka.OFFER_SIDE_BID)
    ask = fetch(duka.OFFER_SIDE_ASK)

    common = bid.index.intersection(ask.index)

    # Use only completed candles.
    valid = [
        t for t in common
        if t.to_pydatetime() + timedelta(minutes=5) <= now
    ]

    if not valid:
        raise RuntimeError("No completed M5 candles")

    bid = bid.loc[valid].copy()
    ask = ask.loc[valid].copy()

    if len(bid) < 200:
        raise RuntimeError("Insufficient M5 history")

    last_time = bid.index[-1]
    age = (now - last_time.to_pydatetime()).total_seconds() / 60

    if age > 20:
        print("Stale market data. No signal.")
        return None, None

    return bid, ask


def detect_signal(bid, ask):
    # Build completed H1 candles.
    h1 = bid.resample("1h").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
    })

    counts = bid["close"].resample("1h").count()
    h1 = h1[counts == 12].dropna()

    if len(h1) < 55:
        print("Insufficient completed H1 history")
        return None

    h1["ema20"] = h1["close"].ewm(
        span=20, adjust=False
    ).mean()

    h1["ema50"] = h1["close"].ewm(
        span=50, adjust=False
    ).mean()

    trend = (
        "BUY"
        if h1["ema20"].iloc[-1] > h1["ema50"].iloc[-1]
        else "SELL"
    )

    # Confirmed M5 pivot: two candles on each side.
    df = bid.copy()

    df["pivot_high"] = (
        (df["high"] > df["high"].shift(1)) &
        (df["high"] > df["high"].shift(2)) &
        (df["high"] > df["high"].shift(-1)) &
        (df["high"] > df["high"].shift(-2))
    )

    df["pivot_low"] = (
        (df["low"] < df["low"].shift(1)) &
        (df["low"] < df["low"].shift(2)) &
        (df["low"] < df["low"].shift(-1)) &
        (df["low"] < df["low"].shift(-2))
    )

    # Last completed M5 candle.
    last = df.iloc[-1]
    previous = df.iloc[-2]

    # A pivot is only available after two subsequent
    # candles have completed.
    confirmed = df.iloc[:-2]

    if len(confirmed) < 20:
        return None

    recent = confirmed.iloc[-48:]

    highs = recent[recent["pivot_high"]]
    lows = recent[recent["pivot_low"]]

    if highs.empty or lows.empty:
        print("No confirmed pivots")
        return None

    resistance = float(highs["high"].iloc[-1])
    support = float(lows["low"].iloc[-1])

    spread = (
        float(ask["close"].iloc[-1]) -
        float(bid["close"].iloc[-1])
    ) / PIP

    if spread < 0 or spread > 2:
        print("Spread filter:", round(spread, 2))
        return None

    direction = None

    if (
        trend == "BUY"
        and previous["close"] <= resistance
        and last["close"] > resistance
    ):
        direction = "BUY"

    elif (
        trend == "SELL"
        and previous["close"] >= support
        and last["close"] < support
    ):
        direction = "SELL"

    if direction is None:
        print("No confirmed signal")
        return None

    entry = (
        float(ask["close"].iloc[-1])
        if direction == "BUY"
        else float(bid["close"].iloc[-1])
    )

    if direction == "BUY":
        stop = support
        risk = entry - stop
        target = entry + 2 * risk
    else:
        stop = resistance
        risk = stop - entry
        target = entry - 2 * risk

    risk_pips = risk / PIP

    if not 5 <= risk_pips <= 30:
        print("Risk filter:", round(risk_pips, 2))
        return None

    candle_time = df.index[-1]

    return {
        "id": f"{candle_time.isoformat()}_{direction}",
        "time": candle_time.isoformat(),
        "direction": direction,
        "entry": round(entry, 5),
        "stop": round(stop, 5),
        "target": round(target, 5),
        "risk_pips": round(risk_pips, 1),
        "spread": round(spread, 1),
    }


def load_state():
    if not STATE_FILE.exists():
        return {"last_signal_id": None}

    return json.loads(STATE_FILE.read_text())


def save_state(signal_id):
    STATE_FILE.write_text(
        json.dumps(
            {"last_signal_id": signal_id},
            indent=2,
        )
    )


async def main():
    print("GBP/USD PAPER SIGNAL MONITOR")

    bid, ask = fetch_data()

    if bid is None:
        return

    print("Latest M5:", bid.index[-1])

    signal = detect_signal(bid, ask)

    if signal is None:
        return

    state = load_state()

    if state.get("last_signal_id") == signal["id"]:
        print("Duplicate signal skipped")
        return

    message = (
        "GBP/USD — PAPER SIGNAL\n\n"
        f"Direction: {signal['direction']}\n"
        f"Time UTC: {signal['time']}\n"
        f"Reference price: {signal['entry']}\n"
        f"Stop Loss: {signal['stop']}\n"
        f"Take Profit: {signal['target']}\n"
        f"Risk distance: {signal['risk_pips']} pips\n"
        f"Spread: {signal['spread']} pips\n"
        "Risk/Reward: 1:2\n\n"
        "Experimental signal only.\n"
        "Not an executed trade."
    )

    async with Bot(TOKEN) as bot:
        await bot.send_message(
            chat_id=CHAT_ID,
            text=message,
        )

    save_state(signal["id"])

    print("PAPER SIGNAL SENT:", signal["id"])


if __name__ == "__main__":
    asyncio.run(main())
