import os
import requests
import yfinance as yf
import pandas as pd
import numpy as np

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

PAIRS = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X",
    "USDCAD": "USDCAD=X",
}

def send_message(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
    r.raise_for_status()

def fetch_1h(ticker):
    df = yf.download(ticker, period="60d", interval="1h", progress=False, auto_adjust=False)
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close"]].copy()
    df.columns = ["open", "high", "low", "close"]
    df.index = pd.to_datetime(df.index, utc=True)
    return df.dropna()

def compute_signal(df):
    daily = df["close"].resample("1D").last().dropna()
    daily_sma = daily.rolling(50).mean()
    trend_daily = np.sign(daily - daily_sma).shift(1)
    df = df.copy()
    df["trend_daily"] = trend_daily.reindex(df.index, method="ffill")
    df["don_hi"] = df["high"].rolling(20).max().shift(1)
    df["don_lo"] = df["low"].rolling(20).min().shift(1)
    pc = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - pc).abs(),
        (df["low"] - pc).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    df["signal"] = 0
    df.loc[(df["close"] > df["don_hi"]) & (df["trend_daily"] > 0), "signal"] = 1
    df.loc[(df["close"] < df["don_lo"]) & (df["trend_daily"] < 0), "signal"] = -1
    return df

def check_pair(name, ticker):
    df = fetch_1h(ticker)
    if df is None or len(df) < 60:
        print(f"[{name}] нет данных")
        return
    sig = compute_signal(df)
    if len(sig) < 3:
        return
    last = sig.iloc[-2]   # последний ЗАКРЫТЫЙ бар (iloc[-1] может быть формирующимся)
    prev = sig.iloc[-3]

    if last["signal"] == 0:
        return
    if prev["signal"] == last["signal"]:
        return  # уже было на предыдущем баре — не дублируем

    hour = last.name.hour
    if hour < 7 or hour >= 18:
        return  # вне сессии Лондон/Нью-Йорк

    side = int(last["signal"])
    entry = float(last["close"])
    atr = float(last["atr"])
    if not np.isfinite(atr) or atr <= 0:
        return
    stop = entry - side * 1.5 * atr
    take = entry + side * 3.0 * atr
    bar_time = last.name.strftime("%Y-%m-%d %H:%M UTC")

    icon = "🟢" if side == 1 else "🔴"
    side_txt = "LONG" if side == 1 else "SHORT"
    text = (
        f"{icon} *{side_txt} {name}*\n\n"
        f"Цена: `{entry:.5f}`\n"
        f"Стоп: `{stop:.5f}`\n"
        f"Тейк: `{take:.5f}`\n"
        f"Бар: {bar_time}"
    )
    send_message(text)
    print(f"[{name}] СИГНАЛ ОТПРАВЛЕН: {side_txt}")
if __name__ == "__main__":
    send_message("🔔 Бот на связи. Жду сигналов.")
        try:
            check_pair(name, ticker)
        except Exception as e:
            print(f"[{name}] ОШИБКА: {e}")
