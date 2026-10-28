
import os
import json
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pandas as pd
import dukascopy_python as duka
from telegram import Bot

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])
STATE_FILE = Path("liquidity_bot_state.json")

LOOKBACK_HOURS = 48
MAX_WAIT_MINUTES = 120
PIVOT_RADIUS = 2

# Поддерживаемые валютные пары
PAIRS = {
    "GBPUSD": "INSTRUMENT_FX_MAJORS_GBP_USD",
    "EURUSD": "INSTRUMENT_FX_MAJORS_EUR_USD",
    "USDJPY": "INSTRUMENT_FX_MAJORS_USD_JPY",
    "USDCHF": "INSTRUMENT_FX_MAJORS_USD_CHF",
    "USDCAD": "INSTRUMENT_FX_MAJORS_USD_CAD",
    "AUDUSD": "INSTRUMENT_FX_MAJORS_AUD_USD",
    "NZDUSD": "INSTRUMENT_FX_MAJORS_NZD_USD",
    "GBPJPY": "INSTRUMENT_FX_CROSSES_GBP_JPY",
    "EURJPY": "INSTRUMENT_FX_CROSSES_EUR_JPY",
    "EURGBP": "INSTRUMENT_FX_CROSSES_EUR_GBP",
    "NZDCHF": "INSTRUMENT_FX_CROSSES_NZD_CHF",
}

logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger("liquidity_bot")


def load_state():
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            LOG.exception("Cannot read state")
    return {
        "offset": 0,
        "levels": {},
        "setups": {},
        "last_bar": {},
        "sent": [],
    }


def save_state(state):
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2))
    temporary.replace(STATE_FILE)


def normalize_pair(value):
    return value.upper().replace("/", "").replace(" ", "")


def decimals(pair):
    return 3 if pair.endswith("JPY") else 5


async def send(bot, message):
    await bot.send_message(chat_id=CHAT_ID, text=message)


async def handle_commands(bot, state):
    updates = await bot.get_updates(
        offset=state.get("offset", 0),
        timeout=0,
        limit=100,
    )

    for update in updates:
        state["offset"] = update.update_id + 1

        message = update.effective_message
        chat = update.effective_chat

        if message is None or chat is None:
            continue

        if str(chat.id) != CHAT_ID:
            continue

        text = (message.text or "").strip()
        parts = text.split()

        if not parts:
            continue

        command = parts[0].split("@")[0].lower()

        if command == "/add":
            if len(parts) != 3:
                await send(bot, "Формат: /add GBPUSD 1.34500")
                continue

            pair = normalize_pair(parts[1])

            if pair not in PAIRS:
                await send(
                    bot,
                    "Пара пока не поддерживается.\n"
                    "Доступно: " + ", ".join(PAIRS),
                )
                continue

            try:
                level = float(parts[2])
                if not 0 < level < 1000:
                    raise ValueError
            except ValueError:
                await send(bot, "Неверная цена уровня.")
                continue

            key = f"{pair}:{level:.{decimals(pair)}f}"

            state["levels"][key] = {
                "pair": pair,
                "level": level,
                "created": datetime.now(timezone.utc).isoformat(),
            }

            state["setups"].pop(key, None)

            await send(
                bot,
                f"Уровень добавлен:\n"
                f"{pair} — {level:.{decimals(pair)}f}",
            )

        elif command == "/remove":
            if len(parts) != 3:
                await send(bot, "Формат: /remove GBPUSD 1.34500")
                continue

            pair = normalize_pair(parts[1])

            try:
                level = float(parts[2])
            except ValueError:
                await send(bot, "Неверная цена.")
                continue

            key = f"{pair}:{level:.{decimals(pair)}f}"

            if key in state["levels"]:
                del state["levels"][key]
                state["setups"].pop(key, None)
                await send(bot, f"Удалён уровень {key}")
            else:
                await send(bot, "Такой уровень не найден.")

        elif command == "/list":
            if not state["levels"]:
                await send(bot, "Уровней пока нет.")
            else:
                await send(
                    bot,
                    "Активные уровни:\n"
                    + "\n".join(sorted(state["levels"])),
                )

        elif command in ("/start", "/help"):
            await send(
                bot,
                "Команды:\n"
                "/add GBPUSD 1.34500\n"
                "/remove GBPUSD 1.34500\n"
                "/list\n"
                "/status",
            )

        elif command == "/status":
            await send(
                bot,
                "Бот работает.\n"
                f"Активных уровней: {len(state['levels'])}\n"
                "Проверка: GitHub Actions, примерно каждые 5 минут.",
            )

    save_state(state)


def fetch_m1(pair):
    instrument = getattr(duka.instruments, PAIRS[pair])

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=LOOKBACK_HOURS)

    df = duka.fetch(
        instrument=instrument,
        interval=duka.INTERVAL_MIN_1,
        offer_side=duka.OFFER_SIDE_BID,
        start=start,
        end=end,
    )

    if df is None or df.empty:
        raise ValueError("No market data")

    df = df.copy()
    df.columns = [str(c).lower() for c in df.columns]

    if "timestamp" in df.columns:
        df = df.set_index("timestamp")

    df.index = pd.to_datetime(df.index, utc=True)
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]

    for column in ("open", "high", "low", "close"):
        df[column] = pd.to_numeric(df[column], errors="coerce")

    df = df.dropna(subset=["open", "high", "low", "close"])

    # Исключаем незакрытую минутную свечу
    cutoff = pd.Timestamp.now(tz="UTC").floor("min")
    return df[df.index < cutoff]


def make_m5(m1):
    counts = m1["close"].resample("5min").count()

    m5 = m1.resample("5min").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
    })

    m5 = m5[counts == 5]

    cutoff = pd.Timestamp.now(tz="UTC").floor("5min")
    return m5[m5.index < cutoff]


def last_confirmed_pivots(frame, index):
    radius = PIVOT_RADIUS
    highs = []
    lows = []

    # Pivot должен быть подтверждён до текущей свечи.
    for j in range(radius, index - radius):
        window = frame.iloc[j - radius:j + radius + 1]
        high = float(frame.iloc[j]["high"])
        low = float(frame.iloc[j]["low"])

        if high == window["high"].max():
            if (window["high"] == high).sum() == 1:
                highs.append(high)

        if low == window["low"].min():
            if (window["low"] == low).sum() == 1:
                lows.append(low)

    return (
        highs[-1] if highs else None,
        lows[-1] if lows else None,
    )


def bos_at(frame, index):
    if index < 2 * PIVOT_RADIUS + 2:
        return None

    previous = float(frame.iloc[index - 1]["close"])
    current = float(frame.iloc[index]["close"])

    high, low = last_confirmed_pivots(frame, index)

    if high is not None and previous <= high < current:
        return "BUY"

    if low is not None and previous >= low > current:
        return "SELL"

    return None


async def analyze_pair(bot, state, pair):
    m1 = fetch_m1(pair)

    if len(m1) < 20:
        LOG.warning("%s: insufficient data", pair)
        return

    now = pd.Timestamp.now(tz="UTC")

    if (now - m1.index[-1]).total_seconds() > 600:
        LOG.warning("%s: stale market data", pair)
        return

    m5 = make_m5(m1)

    previous_bar = state["last_bar"].get(pair)

    if previous_bar:
        last_seen = pd.Timestamp(previous_bar)
    else:
        # При первом запуске не отправляем старые сигналы.
        last_seen = m1.index[-1]

    recent = m1[m1.index > last_seen]

    # Не восстанавливаем старые события после длительного простоя.
    recent = recent[recent.index >= now - pd.Timedelta(minutes=15)]

    pair_levels = {
        key: value
        for key, value in state["levels"].items()
        if value["pair"] == pair
    }

    sent = set(state["sent"])

    for timestamp, candle in recent.iterrows():
        position = m1.index.get_loc(timestamp)

        if position == 0:
            continue

        previous = m1.iloc[position - 1]

        for key, item in pair_levels.items():
            level = item["level"]

            # Не анализируем свечи до добавления уровня.
            created = pd.Timestamp(item["created"])
            if timestamp < created.floor("min"):
                continue

            setup = state["setups"].get(key)

            if setup:
                started = pd.Timestamp(setup["time"])

                if timestamp - started > pd.Timedelta(
                    minutes=MAX_WAIT_MINUTES
                ):
                    state["setups"].pop(key, None)
                    setup = None

            if setup is None:
                upper = (
                    previous["high"] <= level
                    and candle["high"] > level
                )
                lower = (
                    previous["low"] >= level
                    and candle["low"] < level
                )

                if upper == lower:
                    continue

                setup = {
                    "time": timestamp.isoformat(),
                    "direction": "UP" if upper else "DOWN",
                    "sent_tf": [],
                }

                state["setups"][key] = setup

            started = pd.Timestamp(setup["time"])

            for tf, frame in (("M1", m1), ("M5", m5)):
                if tf in setup["sent_tf"]:
                    continue

                if tf == "M1":
                    idx = position
                else:
                    candle_start = timestamp.floor("5min")
                    # M5 можно проверять только после закрытия.
                    if timestamp != candle_start + pd.Timedelta(minutes=4):
                        continue
                    if candle_start not in frame.index:
                        continue
                    idx = frame.index.get_loc(candle_start)

                if idx < 1:
                    continue

                bar_time = frame.index[idx]
                close_time = bar_time + pd.Timedelta(
                    minutes=1 if tf == "M1" else 5
                )

                if close_time <= started:
                    continue

                direction = bos_at(frame, idx)

                if direction is None:
                    continue

                continuation = (
                    (setup["direction"] == "UP" and direction == "BUY")
                    or
                    (setup["direction"] == "DOWN" and direction == "SELL")
                )

                kind = "ПРОДОЛЖЕНИЕ" if continuation else "РАЗВОРОТ"

                signal_id = f"{key}:{setup['time']}:{tf}:{direction}"

                if signal_id in sent:
                    continue

                price = float(frame.iloc[idx]["close"])

                await send(
                    bot,
                    f"LIQUIDITY + BOS\n\n"
                    f"Пара: {pair}\n"
                    f"Уровень: {level:.{decimals(pair)}f}\n"
                    f"Пробой: {setup['direction']}\n"
                    f"BOS: {tf}\n"
                    f"Сигнал: {direction}\n"
                    f"Тип: {kind}\n"
                    f"Цена BOS: {price:.{decimals(pair)}f}\n\n"
                    f"Демо-сигнал. Не является приказом на вход.",
                )

                sent.add(signal_id)
                setup["sent_tf"].append(tf)

    state["last_bar"][pair] = m1.index[-1].isoformat()
    state["sent"] = sorted(sent)[-1000:]


async def main():
    state = load_state()

    for key, default in (
        ("offset", 0),
        ("levels", {}),
        ("setups", {}),
        ("last_bar", {}),
        ("sent", []),
    ):
        state.setdefault(key, default)

    async with Bot(TOKEN) as bot:
        await handle_commands(bot, state)

        pairs = sorted({
            item["pair"] for item in state["levels"].values()
        })

        for pair in pairs:
            try:
                await analyze_pair(bot, state, pair)
            except Exception:
                LOG.exception("Error processing %s", pair)
            finally:
                save_state(state)

    save_state(state)


if __name__ == "__main__":
    asyncio.run(main())
