
import os
import json
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from liquidity_feed import fetch_m1, PAIRS
from telegram import Bot

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
STATE_FILE = Path(os.environ.get("LIQUIDITY_STATE_FILE", "liquidity_bot_state.json"))

MAX_WAIT_MINUTES = 120
PIVOT_RADIUS = 2

logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger("liquidity_bot")
# httpx INFO includes Telegram request URLs containing the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)


def load_state():
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            if isinstance(data, dict):
                for key, kind in (("offset", int), ("levels", dict), ("setups", dict), ("last_bar", dict), ("sent", list)):
                    if not isinstance(data.get(key), kind):
                        raise ValueError(f"Invalid state field: {key}")
                for key, item in data["levels"].items():
                    if (not isinstance(item, dict) or item.get("pair") not in PAIRS
                            or not isinstance(item.get("level"), (int, float))
                            or not 0 < item["level"] < 1000
                            or pd.Timestamp(item.get("created")).tzinfo is None):
                        raise ValueError(f"Invalid level: {key}")
                migrate_state(data)
                return data
        except (OSError, ValueError) as exc:
            raise RuntimeError("Cannot read state; refusing to reset levels") from exc
        raise ValueError("Invalid state object")
    return {
        "offset": 0,
        "levels": {},
        "setups": {},
        "last_bar": {},
        "sent": [],
    }


def migrate_state(state):
    """Never rearm a level that may already have published under the old schema."""
    for key, item in state["levels"].items():
        setup = state["setups"].get(key, {})
        if "status" not in item:
            published = setup.get("sent_tf") or any(
                signal.startswith(key + ":") for signal in state["sent"])
            # Absence from the legacy bounded sent list does not prove a level
            # never signalled. Keep unverifiable registrations blocked.
            item["status"] = "completed" if published else "incomplete_history"
            state["setups"].pop(key, None)
        if item["status"] not in {
            "watching", "waiting_bos", "completed", "expired", "incomplete_history"
        }:
            raise ValueError("Invalid level status")
        if item["status"] == "waiting_bos":
            if (setup.get("direction") not in {"UP", "DOWN"}
                    or not isinstance(setup.get("returned"), bool)
                    or pd.Timestamp(setup.get("time")).tzinfo is None):
                raise ValueError("Invalid pending setup")


def save_state(state):
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2))
    temporary.replace(STATE_FILE)
    if os.environ.get("LIQUIDITY_REMOTE_STATE") == "1":
        from liquidity_state import persist
        persist(STATE_FILE.parent)


def normalize_pair(value):
    return value.upper().replace("/", "").replace(" ", "")


def decimals(pair):
    return 3 if pair.endswith("JPY") else 5


def level_description(item):
    labels = {
        "watching": "ожидание выхода за уровень",
        "waiting_bos": "ожидание BOS M1",
        "completed": "завершён",
        "expired": "срок ожидания истёк",
        "incomplete_history": "заблокирован: неполная история",
    }
    delivery = {"reserved": "результат отправки неизвестен",
                "unknown": "ошибка отправки, повтор отключён",
                "confirmed": "Telegram подтвердил получение"}
    text = labels[item["status"]]
    if "signal" in item:
        text += "; " + delivery[item["signal"]["delivery"]]
    return text


async def send(bot, message):
    await bot.send_message(chat_id=CHAT_ID, text=message)


async def handle_commands(bot, state):
    async def send(bot, message):
        # Persist command effects and Telegram offset before replying.
        save_state(state)
        await globals()["send"](bot, message)

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

        target = parts[0].partition("@")[2]
        if target and target.lower() != str(getattr(bot, "username", "")).lower():
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
                level = round(float(parts[2]), decimals(pair))
                if not 0 < level < 1000:
                    raise ValueError
            except ValueError:
                await send(bot, "Неверная цена уровня.")
                continue

            key = f"{pair}:{level:.{decimals(pair)}f}"

            if key in state["levels"]:
                await send(bot, f"Уровень уже существует: {key}")
                continue

            state["levels"][key] = {
                "pair": pair,
                "level": level,
                "created": datetime.now(timezone.utc).isoformat(),
                "status": "watching",
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
                level = round(float(parts[2]), decimals(pair))
                if pair not in PAIRS or not 0 < level < 1000:
                    raise ValueError
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
                    "Уровни и состояния:\n"
                    + "\n".join(
                        f"{key} — {level_description(item)}"
                        for key, item in sorted(state["levels"].items())),
                )

        elif command in ("/start", "/help"):
            await send(
                bot,
                "Команды:\n"
                "/add GBPUSD 1.34500\n"
                "/remove GBPUSD 1.34500\n"
                "/list\n"
                "/status\n"
                "Повторное наблюдение: /remove, затем /add.",
            )

        elif command == "/status":
            await send(
                bot,
                "Бот работает.\n"
                f"Активных уровней: {sum(item.get('status') in {'watching', 'waiting_bos'} for item in state['levels'].values())}\n"
                + "\n".join(f"{key} — {level_description(item)}"
                            for key, item in sorted(state["levels"].items()))
                + "\nПроверка: GitHub Actions, примерно каждые 5 минут; возможны задержки.\n"
                f"Последние свечи UTC: {state['last_bar']}\n"
                f"Ошибки: {state.get('errors', {})}",
            )

    save_state(state)


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
    for j in range(index - radius - 1, radius - 1, -1):
        window = frame.iloc[j - radius:j + radius + 1]
        high = float(frame.iloc[j]["high"])
        low = float(frame.iloc[j]["low"])

        if high == window["high"].max():
            if (window["high"] == high).sum() == 1:
                if not highs:
                    highs.append(high)

        if low == window["low"].min():
            if (window["low"] == low).sum() == 1:
                if not lows:
                    lows.append(low)
        if highs and lows:
            break

    return (
        highs[-1] if highs else None,
        lows[-1] if lows else None,
    )


def bos_at(frame, index):
    if index < 2 * PIVOT_RADIUS + 2:
        return None

    # Never infer structure across missing market minutes.
    expected = pd.Timedelta(minutes=1)
    gaps = frame.index[:index + 1].to_series().diff() != expected
    start = int(np.flatnonzero(gaps.to_numpy())[-1])
    frame = frame.iloc[start:index + 1]
    index = len(frame) - 1
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


def validate_m1(frame, now):
    """Reject malformed, partial, duplicated or out-of-order candles."""
    if (not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None
            or not frame.index.is_monotonic_increasing or frame.index.has_duplicates
            or not (frame.index == frame.index.floor("min")).all()):
        raise ValueError("Invalid M1 timestamps")
    values = frame[["open", "high", "low", "close"]]
    if (not np.isfinite(values.to_numpy()).all() or (values <= 0).any().any()
            or (frame["high"] < values.max(axis=1)).any()
            or (frame["low"] > values.min(axis=1)).any()
            or (frame.index >= now.floor("min")).any()):
        raise ValueError("Invalid or unclosed M1 candles")


def finish_level(state, key, status):
    state["levels"][key]["status"] = status
    state["setups"].pop(key, None)


async def analyze_pair(bot, state, pair):
    migrate_state(state)
    m1 = fetch_m1(pair)
    now = pd.Timestamp.now(tz="UTC")
    validate_m1(m1, now)
    if len(m1) < 20:
        raise ValueError(f"{pair}: insufficient data")
    if (now - m1.index[-1]).total_seconds() > 600:
        raise ValueError(f"{pair}: stale market data")

    previous_bar = state["last_bar"].get(pair)
    last_seen = pd.Timestamp(previous_bar) if previous_bar else m1.index[-1]
    if last_seen > m1.index[-1]:
        raise ValueError(f"{pair}: market history moved backwards")
    levels = {key: item for key, item in state["levels"].items()
              if item["pair"] == pair and item["status"] in {"watching", "waiting_bos"}}
    for key, item in levels.items():
        # A missing checkpoint means we cannot prove the intervening history.
        if (previous_bar and last_seen < m1.index[0]
                and pd.Timestamp(item["created"]).ceil("min") <= m1.index[0]):
            finish_level(state, key, "incomplete_history")

    for timestamp, candle in m1[m1.index > last_seen].iterrows():
        position = m1.index.get_loc(timestamp)
        for key, item in levels.items():
            if item["status"] not in {"watching", "waiting_bos"}:
                continue
            if timestamp < pd.Timestamp(item["created"]).ceil("min"):
                continue
            if (position == 0 or timestamp - m1.index[position - 1]
                    != pd.Timedelta(minutes=1)):
                finish_level(state, key, "incomplete_history")
                continue
            previous = m1.iloc[position - 1]
            level = item["level"]
            setup = state["setups"].get(key)
            if setup is None:
                upper = previous["high"] <= level < candle["high"]
                lower = previous["low"] >= level > candle["low"]
                if upper == lower:
                    continue
                setup = {"time": timestamp.isoformat(),
                         "direction": "UP" if upper else "DOWN", "returned": False}
                state["setups"][key] = setup
                item["status"] = "waiting_bos"

            started = pd.Timestamp(setup["time"])
            # Pending history must still be available and continuous, even after restart.
            history = m1.loc[started:timestamp]
            expected = pd.date_range(started, timestamp, freq="min")
            if not history.index.equals(expected):
                finish_level(state, key, "incomplete_history")
                continue
            if timestamp - started > pd.Timedelta(minutes=MAX_WAIT_MINUTES):
                finish_level(state, key, "expired")
                continue
            returned = (candle["close"] < level if setup["direction"] == "UP"
                        else candle["close"] > level)
            setup["returned"] = setup["returned"] or bool(returned)
            if timestamp <= started:
                continue
            direction = bos_at(m1, position)
            if direction is None:
                continue
            continuation = ((setup["direction"] == "UP" and direction == "BUY")
                            or (setup["direction"] == "DOWN" and direction == "SELL"))
            kind = "ПРОДОЛЖЕНИЕ" if continuation else "РАЗВОРОТ"
            event = ("снятие с возвратом за уровень" if setup["returned"]
                     else "пробой без возврата")
            # At most one attempt per registered level, persisted BEFORE Telegram.
            # Ambiguous timeouts remain terminal, preventing duplicate delivery.
            item["signal"] = {"time": timestamp.isoformat(), "direction": direction,
                              "event": event, "delivery": "reserved"}
            finish_level(state, key, "completed")
            save_state(state)
            try:
                await send(bot, f"LIQUIDITY + BOS\n\n"
                           f"Пара: {pair}\nУровень: {level:.{decimals(pair)}f}\n"
                           f"Событие: {event}\nПробой: {setup['direction']}\n"
                           f"BOS: M1\nСигнал: {direction}\nТип: {kind}\n"
                           f"Цена BOS: {float(candle['close']):.{decimals(pair)}f}\n"
                           f"Свеча BOS UTC: {timestamp.isoformat()}\n\n"
                           "Демо-сигнал. Не является приказом на вход.")
            except Exception:
                item["signal"]["delivery"] = "unknown"
                save_state(state)
                raise
            item["signal"]["delivery"] = "confirmed"
            save_state(state)

    state["last_bar"][pair] = m1.index[-1].isoformat()
    state.setdefault("errors", {}).pop(pair, None)


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
            except Exception as exc:
                state.setdefault("errors", {})[pair] = type(exc).__name__
                LOG.exception("Error processing %s", pair)
            finally:
                save_state(state)

    state["errors"] = {p: e for p, e in state.get("errors", {}).items() if p in pairs}
    save_state(state)
    if state.get("errors"):
        raise RuntimeError("One or more pairs failed; see logs and /status")


if __name__ == "__main__":
    asyncio.run(main())

