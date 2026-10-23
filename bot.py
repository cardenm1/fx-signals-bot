
import os
import json
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import dukascopy_python as duka
from telegram import Bot

# ==========================================
# CONFIGURATION
# ==========================================

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

PAIRS = {
    "GBP/USD": {
        "instrument": "INSTRUMENT_FX_MAJORS_GBP_USD",
        "pip": 0.0001,
    },
    "GBP/JPY": {
        "instrument": "INSTRUMENT_FX_MAJORS_GBP_JPY",
        "pip": 0.01,
    },
    "EUR/JPY": {
        "instrument": "INSTRUMENT_FX_MAJORS_EUR_JPY",
        "pip": 0.01,
    },
}

RR = 2.0
MIN_SL_PIPS = 5
MAX_SL_PIPS = 30
MAX_SPREAD_PIPS = 2
HISTORY_DAYS = 5
PIVOT_BARS = 2
STATE_FILE = Path("signal_state.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# ==========================================
# STATE
# ==========================================

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (ValueError, OSError):
            pass
    return {}

def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, indent=2)
    )

# ==========================================
# MARKET DATA
# ==========================================

def get_data(instrument_name, side):
    instrument = getattr(
        duka.instruments, instrument_name
    )

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=HISTORY_DAYS)

    df = duka.fetch(
        instrument=instrument,
        interval=duka.INTERVAL_MIN_5,
        offer_side=side,
        start=start,
        end=end,
    )

    if df is None or df.empty:
        raise ValueError("No market data")

    df = df.copy()

    df.columns = [
        str(c).lower() for c in df.columns
    ]

    if "timestamp" in df.columns:
        df = df.set_index("timestamp")

    df.index = pd.to_datetime(
        df.index, utc=True
    )

    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]

    required = ["open", "high", "low", "close"]

    for col in required:
        if col not in df.columns:
            raise ValueError(f"Missing column: {col}")

        df[col] = pd.to_numeric(
            df[col], errors="coerce"
        )

    df = df.dropna(subset=required)

    # Only completed M5 candles
    cutoff = pd.Timestamp.now(tz="UTC").floor("5min")
    df = df[df.index < cutoff]

    return df

# ==========================================
# H1 TREND
# ==========================================

def get_trend(m5):
    h1 = m5.resample(
        "1h", label="left", closed="left"
    ).agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
    }).dropna()

    cutoff = pd.Timestamp.now(tz="UTC").floor("1h")
    h1 = h1[h1.index < cutoff]

    if len(h1) < 50:
        return None

    h1["ema20"] = h1["close"].ewm(
        span=20, adjust=False
    ).mean()

    h1["ema50"] = h1["close"].ewm(
        span=50, adjust=False
    ).mean()

    last = h1.iloc[-1]

    if last["ema20"] > last["ema50"]:
        return "BUY"

    if last["ema20"] < last["ema50"]:
        return "SELL"

    return None

# ==========================================
# CONFIRMED M5 PIVOTS
# ==========================================

def get_pivots(df):
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()

    pivot_highs = []
    pivot_lows = []

    n = PIVOT_BARS

    for i in range(n, len(df) - n):
        high_window = highs[i-n:i+n+1]
        low_window = lows[i-n:i+n+1]

        if highs[i] == max(high_window):
            if np.sum(high_window == highs[i]) == 1:
                pivot_highs.append(i)

        if lows[i] == min(low_window):
            if np.sum(low_window == lows[i]) == 1:
                pivot_lows.append(i)

    return pivot_highs, pivot_lows

# ==========================================
# SIGNAL ENGINE
# ==========================================

def analyze(pair, bid, ask, pip):
    common = bid.index.intersection(ask.index)

    bid = bid.loc[common].copy()
    ask = ask.loc[common].copy()

    if len(bid) < 100:
        return None

    # Avoid signals on old candles
    age = (
        pd.Timestamp.now(tz="UTC") - bid.index[-1]
    ).total_seconds()

    if age > 900:
        return None

    trend = get_trend(bid)

    if trend is None:
        return None

    pivot_highs, pivot_lows = get_pivots(bid)

    if not pivot_highs or not pivot_lows:
        return None

    last_i = len(bid) - 1
    last_bid = bid.iloc[-1]
    last_ask = ask.iloc[-1]

    spread = (
        last_ask["close"] - last_bid["close"]
    ) / pip

    if spread < 0 or spread > MAX_SPREAD_PIPS:
        return None

    # Only pivots confirmed before signal candle
    highs = [i for i in pivot_highs if i < last_i]
    lows = [i for i in pivot_lows if i < last_i]

    if not highs or not lows:
        return None

    last_high_i = highs[-1]
    last_low_i = lows[-1]

    swing_high = float(bid.iloc[last_high_i]["high"])
    swing_low = float(bid.iloc[last_low_i]["low"])

    previous_close = float(bid.iloc[-2]["close"])
    current_close = float(last_bid["close"])

    direction = None
    entry = None
    stop = None

    if trend == "BUY":
        if (
            previous_close <= swing_high
            and current_close > swing_high
        ):
            direction = "BUY"
            entry = float(last_ask["close"])
            stop = swing_low

    elif trend == "SELL":
        if (
            previous_close >= swing_low
            and current_close < swing_low
        ):
            direction = "SELL"
            entry = float(last_bid["close"])
            stop = swing_high + spread * pip

    if direction is None:
        return None

    risk = abs(entry - stop)
    risk_pips = risk / pip

    if not MIN_SL_PIPS <= risk_pips <= MAX_SL_PIPS:
        return None

    if direction == "BUY":
        if stop >= entry:
            return None
        target = entry + RR * risk
    else:
        if stop <= entry:
            return None
        target = entry - RR * risk

    digits = 3 if pip == 0.01 else 5

    signal_time = bid.index[-1].isoformat()

    return {
        "pair": pair,
        "direction": direction,
        "entry": round(entry, digits),
        "sl": round(stop, digits),
        "tp": round(target, digits),
        "risk_pips": round(risk_pips, 1),
        "spread_pips": round(spread, 1),
        "time": signal_time,
        "id": f"{pair}:{direction}:{signal_time}",
    }

# ==========================================
# TELEGRAM
# ==========================================

def format_signal(s):
    return (
        "PAPER SIGNAL | DEMO\n\n"
        f"Pair: {s['pair']}\n"
        f"Direction: {s['direction']}\n\n"
        f"Entry: {s['entry']}\n"
        f"Stop Loss: {s['sl']}\n"
        f"Take Profit: {s['tp']}\n\n"
        f"Risk: {s['risk_pips']} pips\n"
        f"Spread: {s['spread_pips']} pips\n"
        f"Reward/Risk: 2:1\n\n"
        "Strategy: H1 EMA20/50 + M5 BOS\n"
        f"Signal candle UTC: {s['time']}\n\n"
        "Demo testing only. Not an executed trade."
    )

# ==========================================
# MAIN
# ==========================================

async def main():
    state = load_state()
    bot = Bot(token=TOKEN)

    for pair, config in PAIRS.items():
        try:
            logging.info("Checking %s", pair)

            bid = get_data(
                config["instrument"],
                duka.OFFER_SIDE_BID
            )

            ask = get_data(
                config["instrument"],
                duka.OFFER_SIDE_ASK
            )

            signal = analyze(
                pair, bid, ask, config["pip"]
            )

            if signal is None:
                logging.info("%s: no signal", pair)
                continue

            if state.get(pair) == signal["id"]:
                logging.info("%s: duplicate", pair)
                continue

            await bot.send_message(
                chat_id=CHAT_ID,
                text=format_signal(signal)
            )

            state[pair] = signal["id"]
            save_state(state)

            logging.info("%s: signal sent", pair)

        except Exception:
            logging.exception(
                "Error while checking %s", pair
            )

    logging.info("Finished checking all pairs")

if __name__ == "__main__":
    asyncio.run(main())
