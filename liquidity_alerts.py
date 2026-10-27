"""Liquidity sweep -> first BOS -> opposite structure break alerts.
Research/demo notifications only; no order placement.
Run: python liquidity_alerts.py
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import dukascopy_python as duka
from dukascopy_python import instruments
from telegram import Bot

PAIRS = {
    "USD/CAD": ("INSTRUMENT_FX_MAJORS_USD_CAD", 5),
    "NZD/CHF": ("INSTRUMENT_FX_CROSSES_NZD_CHF", 5),
    "GBP/USD": ("INSTRUMENT_FX_MAJORS_GBP_USD", 5),
    "EUR/JPY": ("INSTRUMENT_FX_CROSSES_EUR_JPY", 3),
}
STATE_PATH = Path(os.getenv("LIQUIDITY_STATE_PATH", "liquidity_alert_state.json"))
LOOKBACK_DAYS = 35
MAX_SETUP_HOURS = 48
PIVOT_RADIUS = 2
LOG = logging.getLogger("liquidity_alerts")


def fetch_m1(instrument_name):
    instrument = getattr(instruments, instrument_name)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=LOOKBACK_DAYS)
    data = duka.fetch(instrument=instrument, interval=duka.INTERVAL_MIN_1,
                      offer_side=duka.OFFER_SIDE_BID, start=start, end=end)
    if data is None or data.empty:
        raise ValueError("No M1 data")
    data = data.copy()
    data.columns = [str(c).lower() for c in data.columns]
    if "timestamp" in data.columns:
        data = data.set_index("timestamp")
    data.index = pd.to_datetime(data.index, utc=True)
    data = data.sort_index()
    data = data[~data.index.duplicated(keep="last")]
    cols = ["open", "high", "low", "close"]
    for col in cols:
        data[col] = pd.to_numeric(data[col], errors="coerce")
    data = data.dropna(subset=cols)
    data = data[data.index < pd.Timestamp.now(tz="UTC").floor("min")]
    if data.empty:
        raise ValueError("No closed M1 bars")
    return data


def aggregate(m1, rule):
    counts = m1["close"].resample(rule, label="left", closed="left").count()
    frame = m1.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"})
    expected = {"1min": 1, "5min": 5, "1h": 60, "4h": 240, "1D": 1440}[rule]
    # Forex daily candles have weekend gaps; only require that the period has ended,
    # and that a substantial portion of intraday observations exists.
    minimum = expected if rule in ("1min", "5min", "1h", "4h") else 60
    frame = frame[counts >= minimum]
    now = pd.Timestamp.now(tz="UTC")
    frame = frame[frame.index + pd.Timedelta(rule) <= now]
    return frame


def pivots(df, radius=PIVOT_RADIUS):
    highs, lows = [], []
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    for i in range(radius, len(df) - radius):
        if h[i] > max(h[i-radius:i]) and h[i] > max(h[i+1:i+radius+1]):
            highs.append(i)
        if l[i] < min(l[i-radius:i]) and l[i] < min(l[i+1:i+radius+1]):
            lows.append(i)
    return highs, lows


def find_sweeps(frame, tf):
    """Sweep: wick beyond a confirmed earlier swing, close back inside."""
    high_p, low_p = pivots(frame)
    events = []
    for i in range(2 * PIVOT_RADIUS + 1, len(frame)):
        earlier_highs = [p for p in high_p if p + PIVOT_RADIUS < i]
        earlier_lows = [p for p in low_p if p + PIVOT_RADIUS < i]
        bar = frame.iloc[i]
        if earlier_highs:
            p = earlier_highs[-1]
            level = float(frame.iloc[p]["high"])
            if bar["high"] > level and bar["close"] < level:
                events.append({"tf": tf, "side": "upper", "time": frame.index[i].isoformat(),
                               "level": level})
        if earlier_lows:
            p = earlier_lows[-1]
            level = float(frame.iloc[p]["low"])
            if bar["low"] < level and bar["close"] > level:
                events.append({"tf": tf, "side": "lower", "time": frame.index[i].isoformat(),
                               "level": level})
    return events


def find_reversal(micro, event):
    """After sweep: first BOS in sweep's reversal direction, then opposite break.

    Upper sweep: first bearish BOS through confirmed swing low, then bullish
    break through confirmed swing high. Lower sweep is symmetrical.
    """
    start = pd.Timestamp(event["time"])
    if event["tf"] == "H1":
        start += pd.Timedelta(hours=1)
    elif event["tf"] == "H4":
        start += pd.Timedelta(hours=4)
    else:
        start += pd.Timedelta(days=1)
    stop = start + pd.Timedelta(hours=MAX_SETUP_HOURS)
    view = micro[(micro.index >= start) & (micro.index <= stop)]
    if len(view) < 8:
        return None
    hp, lp = pivots(micro)
    first_direction = "bearish" if event["side"] == "upper" else "bullish"
    first = None
    for pos in range(1, len(view)):
        t = view.index[pos]
        idx = micro.index.get_loc(t)
        previous = micro.iloc[idx - 1]["close"]
        current = micro.iloc[idx]["close"]
        if first is None:
            candidates = lp if first_direction == "bearish" else hp
            valid = [p for p in candidates if p + PIVOT_RADIUS < idx]
            if not valid:
                continue
            level = float(micro.iloc[valid[-1]]["low" if first_direction == "bearish" else "high"])
            crossed = (previous >= level and current < level) if first_direction == "bearish" else (previous <= level and current > level)
            if crossed:
                first = (idx, t)
            continue
        # Reverse break must be against the first BOS and use a swing confirmed
        # after the first BOS (not an older historical pivot).
        candidates = hp if first_direction == "bearish" else lp
        valid = [p for p in candidates if p >= first[0] and p + PIVOT_RADIUS < idx]
        if not valid:
            continue
        level = float(micro.iloc[valid[-1]]["high" if first_direction == "bearish" else "low"])
        crossed = (previous <= level and current > level) if first_direction == "bearish" else (previous >= level and current < level)
        if crossed:
            return {"direction": "bullish" if first_direction == "bearish" else "bearish",
                    "first_bos": first[1].isoformat(), "break_time": t.isoformat(),
                    "price": float(current), "level": level}
    return None


def analyze(pair, m1):
    now = pd.Timestamp.now(tz="UTC")
    if (now - m1.index[-1]).total_seconds() > 600:
        LOG.warning("%s stale M1 feed", pair)
        return []
    events = []
    for tf, rule in (("D1", "1D"), ("H4", "4h"), ("H1", "1h")):
        frame = aggregate(m1, rule)
        if len(frame) < 8:
            continue
        for event in find_sweeps(frame, tf):
            timestamp = pd.Timestamp(event["time"])
            if now - timestamp > pd.Timedelta(hours=MAX_SETUP_HOURS + 25):
                continue
            for micro_tf, micro_rule in (("M1", "1min"), ("M5", "5min")):
                micro = aggregate(m1, micro_rule)
                reversal = find_reversal(micro, event)
                if reversal is None:
                    continue
                if now - pd.Timestamp(reversal["break_time"]) > pd.Timedelta(minutes=10):
                    continue
                identity = f"{pair}:{tf}:{event['time']}:{micro_tf}:{reversal['break_time']}"
                events.append(dict(id=identity, pair=pair, micro_tf=micro_tf,
                                   sweep=event, reversal=reversal))
    return events


def format_alert(event, digits):
    sweep = event["sweep"]
    reversal = event["reversal"]
    uk = pd.Timestamp(reversal["break_time"]).tz_convert(ZoneInfo("Europe/London"))
    return (
        "LIQUIDITY + STRUCTURE ALERT (DEMO)\n"
        f"Pair: {event['pair']}\n"
        f"Liquidity: {sweep['tf']} / {sweep['side']}\n"
        f"Swept level: {sweep['level']:.{digits}f}\n"
        f"First BOS: {reversal['first_bos']} UTC\n"
        f"Opposite break: {event['micro_tf']} / {reversal['direction']}\n"
        f"Break level: {reversal['level']:.{digits}f}\n"
        f"Close price: {reversal['price']:.{digits}f}\n"
        f"Time (UK): {uk.strftime('%Y-%m-%d %H:%M %Z')}\n"
        "Notification only. No trade executed."
    )


def load_state():
    if not STATE_PATH.exists():
        return {}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


async def main():
    logging.basicConfig(level=logging.INFO)
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    state = load_state()
    save_state(state)
    bot = Bot(token=token)
    for pair, (instrument, digits) in PAIRS.items():
        try:
            alerts = analyze(pair, fetch_m1(instrument))
            for event in sorted(alerts, key=lambda e: e["reversal"]["break_time"]):
                if event["id"] in state:
                    continue
                await bot.send_message(chat_id=chat_id, text=format_alert(event, digits))
                state[event["id"]] = datetime.now(timezone.utc).isoformat()
                save_state(state)
            LOG.info("%s: %d candidate alerts", pair, len(alerts))
        except Exception:
            LOG.exception("Failed: %s", pair)


if __name__ == "__main__":
    asyncio.run(main())
