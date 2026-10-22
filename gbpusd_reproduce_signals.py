"""Reproduce original GBPUSD research signals from yearlong BID/ASK CSVs.
Run in Google Colab after mounting Drive. This is NOT a live alert bot.
"""
from pathlib import Path
import pandas as pd
folder = Path('/content/drive/MyDrive/GBPUSD_Research')
def read_side(side):
    f=folder / f'GBPUSD_{side}_M5_CLEAN.csv'
    if not f.exists():
        raise FileNotFoundError(f)
    df=pd.read_csv(f, index_col=0, parse_dates=True)
    df.index=pd.to_datetime(df.index, utc=True)
    return df.sort_index()
bid_clean=read_side('BID')
ask_clean=read_side('ASK')
assert bid_clean.index.equals(ask_clean.index), 'BID/ASK timestamps differ'
print('Loaded', len(bid_clean), 'BID/ASK candles')


# ===== ORIGINAL NOTEBOOK CELL 14 =====
import pandas as pd

# Создаём H1 из BID M5
h1_year = bid_clean[
    ["open", "high", "low", "close"]
].resample(
    "1h",
    label="left",
    closed="left"
).agg({
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last"
})

# Считаем количество M5-свечей внутри каждого часа
h1_year["M5_Count"] = (
    bid_clean["close"]
    .resample("1h", label="left", closed="left")
    .count()
)

# Оставляем только полные часы
h1_year = h1_year[
    h1_year["M5_Count"] == 12
].copy()

# Разделяем историю на непрерывные участки
gap_hours = (
    h1_year.index.to_series()
    .diff()
    .dt.total_seconds()
    .div(3600)
)

h1_year["Segment"] = (
    gap_hours.fillna(1) > 1
).cumsum().astype(int)

# Переводим время в британский часовой пояс
h1_year.index = h1_year.index.tz_convert(
    "Europe/London"
)

print("H1 свечей:", len(h1_year))
print("Начало:", h1_year.index.min())
print("Конец:", h1_year.index.max())
print("Непрерывных сегментов:", h1_year["Segment"].nunique())

print("\nПервые 5 свечей:")
print(h1_year.head())


# ===== ORIGINAL NOTEBOOK CELL 18 =====
import pandas as pd

h1_strategy = h1_year.copy().sort_index()

# EMA сохраняет историю через выходные
h1_strategy["EMA50"] = (
    h1_strategy["close"]
    .ewm(span=50, adjust=False)
    .mean()
)

h1_strategy["EMA200"] = (
    h1_strategy["close"]
    .ewm(span=200, adjust=False)
    .mean()
)

# Период прогрева
h1_strategy["Warmup_OK"] = False
h1_strategy.iloc[200:, h1_strategy.columns.get_loc("Warmup_OK")] = True

# Направление тренда
h1_strategy["Trend"] = "neutral"

valid = h1_strategy["Warmup_OK"]

h1_strategy.loc[
    valid & (h1_strategy["EMA50"] > h1_strategy["EMA200"]),
    "Trend"
] = "up"

h1_strategy.loc[
    valid & (h1_strategy["EMA50"] < h1_strategy["EMA200"]),
    "Trend"
] = "down"

h1_strategy["EMA_Gap_Pips"] = (
    (h1_strategy["EMA50"] - h1_strategy["EMA200"]).abs()
    / 0.0001
)

print("Всего H1:", len(h1_strategy))
print("После прогрева:", valid.sum())

print("\nНаправление тренда:")
print(h1_strategy.loc[valid, "Trend"].value_counts())

print("\nРасстояние EMA после прогрева:")
print(
    h1_strategy.loc[valid, "EMA_Gap_Pips"]
    .describe().round(2)
)


# ===== ORIGINAL NOTEBOOK CELL 19 =====
import pandas as pd

h1_pivots = h1_strategy.copy()

# Находим локальные максимумы и минимумы
# Только внутри непрерывных сегментов
h1_pivots["Pivot_High"] = False
h1_pivots["Pivot_Low"] = False

for segment, group in h1_pivots.groupby("Segment"):

    highs = group["high"]
    lows = group["low"]

    pivot_high = (
        (highs > highs.shift(1)) &
        (highs > highs.shift(2)) &
        (highs > highs.shift(-1)) &
        (highs > highs.shift(-2))
    )

    pivot_low = (
        (lows < lows.shift(1)) &
        (lows < lows.shift(2)) &
        (lows < lows.shift(-1)) &
        (lows < lows.shift(-2))
    )

    h1_pivots.loc[group.index, "Pivot_High"] = pivot_high.fillna(False)
    h1_pivots.loc[group.index, "Pivot_Low"] = pivot_low.fillna(False)

# Отмечаем время подтверждения:
# две следующие H1-свечи должны закрыться
h1_pivots["High_Confirmed"] = (
    h1_pivots.groupby("Segment")["Pivot_High"]
    .shift(2, fill_value=False)
)

h1_pivots["Low_Confirmed"] = (
    h1_pivots.groupby("Segment")["Pivot_Low"]
    .shift(2, fill_value=False)
)

print("Всего H1-свечей:", len(h1_pivots))
print("Pivot High:", h1_pivots["Pivot_High"].sum())
print("Pivot Low:", h1_pivots["Pivot_Low"].sum())

print("Подтверждённых High:", h1_pivots["High_Confirmed"].sum())
print("Подтверждённых Low:", h1_pivots["Low_Confirmed"].sum())

print("\nПервые подтверждения:")
print(
    h1_pivots.loc[
        h1_pivots["High_Confirmed"] |
        h1_pivots["Low_Confirmed"],
        ["high", "low", "High_Confirmed", "Low_Confirmed"]
    ].head(10)
)


# ===== ORIGINAL NOTEBOOK CELL 20 =====
import pandas as pd

events = []

for segment, group in h1_pivots.groupby("Segment"):

    for i in range(2, len(group)):

        row = group.iloc[i]
        pivot = group.iloc[i - 2]

        if row["High_Confirmed"]:
            events.append({
                "Segment": segment,
                "Pivot_Time": group.index[i - 2],
                "Confirmed_Time": group.index[i] + pd.Timedelta(hours=1),
                "Type": "high",
                "Price": pivot["high"]
            })

        if row["Low_Confirmed"]:
            events.append({
                "Segment": segment,
                "Pivot_Time": group.index[i - 2],
                "Confirmed_Time": group.index[i] + pd.Timedelta(hours=1),
                "Type": "low",
                "Price": pivot["low"]
            })

events = pd.DataFrame(events).sort_values(
    ["Segment", "Pivot_Time", "Type"]
).reset_index(drop=True)

impulses = []

for segment, group in events.groupby("Segment"):

    group = group.reset_index(drop=True)

    for i in range(1, len(group)):

        previous = group.iloc[i - 1]
        current = group.iloc[i]

        if previous["Type"] == current["Type"]:
            continue

        direction = (
            "up" if previous["Type"] == "low"
            else "down"
        )

        size_pips = abs(
            current["Price"] - previous["Price"]
        ) / 0.0001

        # Импульс доступен только после подтверждения обоих экстремумов
        available_time = max(
            previous["Confirmed_Time"],
            current["Confirmed_Time"]
        )

        impulses.append({
            "Segment": segment,
            "Start_Time": previous["Pivot_Time"],
            "End_Time": current["Pivot_Time"],
            "Available_Time": available_time,
            "Direction": direction,
            "Start_Price": previous["Price"],
            "End_Price": current["Price"],
            "Range_Pips": size_pips
        })

impulses_year = pd.DataFrame(impulses)

print("Всего импульсов:", len(impulses_year))

print("\nНаправления:")
print(impulses_year["Direction"].value_counts())

print("\nРазмер импульсов:")
print(impulses_year["Range_Pips"].describe().round(2))

print("\nПервые 5 импульсов:")
print(impulses_year.head().to_string(index=False))


# ===== ORIGINAL NOTEBOOK CELL 21 =====
import pandas as pd

df_check = impulses_year.copy()

# Проверка направления
wrong_up = (
    (df_check["Direction"] == "up") &
    (df_check["End_Price"] <= df_check["Start_Price"])
)

wrong_down = (
    (df_check["Direction"] == "down") &
    (df_check["End_Price"] >= df_check["Start_Price"])
)

# Проверка последовательности времени
wrong_time = (
    df_check["End_Time"] <= df_check["Start_Time"]
)

# Проверка доступности информации
wrong_confirmation = (
    df_check["Available_Time"] <
    df_check["End_Time"] + pd.Timedelta(hours=3)
)

# Проверка диапазонов
wrong_range = (
    df_check["Range_Pips"] <= 0
)

print("ПРОВЕРКА H1 ИМПУЛЬСОВ")
print("-------------------------")

print("Всего:", len(df_check))
print("Ошибок UP:", wrong_up.sum())
print("Ошибок DOWN:", wrong_down.sum())
print("Ошибок времени:", wrong_time.sum())
print("Ошибок подтверждения:", wrong_confirmation.sum())
print("Нулевых диапазонов:", wrong_range.sum())

print("\nРазмер менее 5 pip:",
      (df_check["Range_Pips"] < 5).sum())

print("Размер более 100 pip:",
      (df_check["Range_Pips"] > 100).sum())

print("\nВсего импульсов с логическими ошибками:",
      (
          wrong_up |
          wrong_down |
          wrong_time |
          wrong_confirmation |
          wrong_range
      ).sum())


# ===== ORIGINAL NOTEBOOK CELL 22 =====
# Оставляем импульсы без выявленных логических ошибок
valid_mask = ~(
    wrong_up |
    wrong_down |
    wrong_time |
    wrong_confirmation |
    wrong_range
)

impulses_valid = (
    impulses_year.loc[valid_mask]
    .copy()
    .sort_values(["Available_Time", "Start_Time"])
    .reset_index(drop=True)
)

print("ПРОВЕРЕННЫЕ H1 ИМПУЛЬСЫ")
print("-------------------------")

print("Исходных:", len(impulses_year))
print("Исключено:", len(impulses_year) - len(impulses_valid))
print("Осталось:", len(impulses_valid))

print("\nНаправления:")
print(impulses_valid["Direction"].value_counts())

print("\nРазмер импульсов:")
print(impulses_valid["Range_Pips"].describe().round(2))

print("\nПервые 5:")
print(impulses_valid.head().to_string(index=False))


# ===== ORIGINAL NOTEBOOK CELL 23 =====
import pandas as pd

fib_check = impulses_valid.copy()

# Уровень коррекции 50%
fib_check["Fib50"] = (
    fib_check["Start_Price"] +
    fib_check["End_Price"]
) / 2

# Приводим M5 к британскому времени
m5_bid = bid_clean.copy()
m5_bid.index = m5_bid.index.tz_convert("Europe/London")

results = []

for row in fib_check.itertuples():

    # Проверяем движение от конца импульса
    # до момента его подтверждения
    candles = m5_bid.loc[
        (m5_bid.index >= row.End_Time + pd.Timedelta(hours=1)) &
        (m5_bid.index < row.Available_Time)
    ]

    if row.Direction == "up":
        touched = (candles["low"] <= row.Fib50).any()
    else:
        touched = (candles["high"] >= row.Fib50).any()

    results.append(bool(touched))

fib_check["Touched_Before_Confirmation"] = results

print("Всего импульсов:", len(fib_check))

print(
    "Fib 0.5 достигнут до подтверждения:",
    fib_check["Touched_Before_Confirmation"].sum()
)

print(
    "Fib 0.5 ещё не достигнут:",
    (~fib_check["Touched_Before_Confirmation"]).sum()
)

print("\nДоля ранних касаний:")
print(
    round(
        fib_check["Touched_Before_Confirmation"].mean() * 100,
        2
    ),
    "%"
)


# ===== ORIGINAL NOTEBOOK CELL 24 =====
import pandas as pd

fib_signals = fib_check.copy()

touch_times = []
touch_spreads = []

# ASK используется для оценки спреда
m5_ask = ask_clean.copy()
m5_ask.index = m5_ask.index.tz_convert("Europe/London")

for row in fib_signals.itertuples():

    start = row.Available_Time
    end = start + pd.Timedelta(hours=12)

    candles = m5_bid.loc[
        (m5_bid.index >= start) &
        (m5_bid.index < end)
    ]

    # Ищем первое касание после подтверждения
    if row.Direction == "up":
        touches = candles[
            candles["low"] <= row.Fib50
        ]
    else:
        touches = candles[
            candles["high"] >= row.Fib50
        ]

    if touches.empty:
        touch_times.append(pd.NaT)
        touch_spreads.append(float("nan"))
        continue

    first_time = touches.index[0]
    touch_times.append(first_time)

    # Спред на закрытии M5-свечи касания
    if first_time in m5_ask.index:
        spread = (
            m5_ask.loc[first_time, "close"] -
            m5_bid.loc[first_time, "close"]
        ) / 0.0001
    else:
        spread = float("nan")

    touch_spreads.append(spread)

fib_signals["Touch_Time"] = touch_times
fib_signals["Touch_Spread_Pips"] = touch_spreads

fib_signals["Touched_After"] = (
    fib_signals["Touch_Time"].notna()
)

print("Всего импульсов:", len(fib_signals))

print(
    "Касаний после подтверждения:",
    fib_signals["Touched_After"].sum()
)

print(
    "Без касания за 12 часов:",
    (~fib_signals["Touched_After"]).sum()
)

print("\nИз 309 без раннего касания:")
fresh = fib_signals[
    ~fib_signals["Touched_Before_Confirmation"]
]

print(
    "Коснулись после подтверждения:",
    fresh["Touched_After"].sum()
)

print("\nСпред на M5-свечах касания:")
print(
    fib_signals.loc[
        fib_signals["Touched_After"],
        "Touch_Spread_Pips"
    ].describe().round(2)
)


# ===== ORIGINAL NOTEBOOK CELL 26 =====
import pandas as pd

# Сортируем импульсы в порядке их подтверждения
fib_active = (
    fib_signals
    .sort_values(["Segment", "Available_Time", "Start_Time"])
    .reset_index(drop=True)
    .copy()
)

# Следующий подтверждённый импульс
fib_active["Next_Impulse_Time"] = (
    fib_active.groupby("Segment")["Available_Time"]
    .shift(-1)
)

# Касание должно происходить после подтверждения
correct_order = (
    fib_active["Touch_Time"] >=
    fib_active["Available_Time"]
)

# Касание должно быть раньше следующего импульса
before_next = (
    fib_active["Next_Impulse_Time"].isna() |
    (
        fib_active["Touch_Time"] <
        fib_active["Next_Impulse_Time"]
    )
)

fib_active["Touch_While_Active"] = (
    fib_active["Touched_After"] &
    correct_order &
    before_next
)

fib_active["Fresh_Active_Touch"] = (
    fib_active["Touch_While_Active"] &
    ~fib_active["Touched_Before_Confirmation"]
)

signals_ready = fib_active[
    fib_active["Fresh_Active_Touch"]
].copy()

# Дополнительные проверки
wrong_order = (
    signals_ready["Touch_Time"] <
    signals_ready["Available_Time"]
).sum()

wrong_direction = (
    (
        (signals_ready["Direction"] == "up") &
        (signals_ready["End_Price"] <= signals_ready["Start_Price"])
    ) |
    (
        (signals_ready["Direction"] == "down") &
        (signals_ready["End_Price"] >= signals_ready["Start_Price"])
    )
).sum()

print("ПРОВЕРКА ХРОНОЛОГИИ")
print("------------------------")

print("Свежих актуальных касаний:", len(signals_ready))
print("Ошибок порядка событий:", wrong_order)
print("Ошибок направления:", wrong_direction)

print("\nНаправления:")
print(signals_ready["Direction"].value_counts())

print("\nПервые 5 сигналов:")
print(
    signals_ready[
        [
            "Direction",
            "Available_Time",
            "Touch_Time",
            "Fib50",
            "Range_Pips"
        ]
    ].head().to_string(index=False)
)


# ===== ORIGINAL NOTEBOOK CELL 27 =====
import pandas as pd

m5_structure = m5_bid.copy().sort_index()

# Разделяем непрерывные участки истории
gap_minutes = (
    m5_structure.index.to_series()
    .diff()
    .dt.total_seconds()
    .div(60)
)

m5_structure["Segment"] = (
    gap_minutes.fillna(5) > 5
).cumsum()

m5_structure["Pivot_High"] = False
m5_structure["Pivot_Low"] = False

for segment, group in m5_structure.groupby("Segment"):

    high = group["high"]
    low = group["low"]

    pivot_high = (
        (high > high.shift(1)) &
        (high > high.shift(2)) &
        (high > high.shift(-1)) &
        (high > high.shift(-2))
    )

    pivot_low = (
        (low < low.shift(1)) &
        (low < low.shift(2)) &
        (low < low.shift(-1)) &
        (low < low.shift(-2))
    )

    m5_structure.loc[group.index, "Pivot_High"] = (
        pivot_high.fillna(False)
    )

    m5_structure.loc[group.index, "Pivot_Low"] = (
        pivot_low.fillna(False)
    )

# Экстремум подтверждается через 2 свечи
m5_structure["High_Confirmed"] = (
    m5_structure.groupby("Segment")["Pivot_High"]
    .shift(2, fill_value=False)
)

m5_structure["Low_Confirmed"] = (
    m5_structure.groupby("Segment")["Pivot_Low"]
    .shift(2, fill_value=False)
)

# Цена подтверждённого экстремума
m5_structure["Confirmed_High_Price"] = (
    m5_structure.groupby("Segment")["high"].shift(2)
    .where(m5_structure["High_Confirmed"])
)

m5_structure["Confirmed_Low_Price"] = (
    m5_structure.groupby("Segment")["low"].shift(2)
    .where(m5_structure["Low_Confirmed"])
)

print("ПРОВЕРКА M5 СТРУКТУРЫ")
print("-------------------------")

print("Всего M5-свечей:", len(m5_structure))

print(
    "Подтверждённых максимумов:",
    m5_structure["High_Confirmed"].sum()
)

print(
    "Подтверждённых минимумов:",
    m5_structure["Low_Confirmed"].sum()
)

print(
    "Непрерывных сегментов:",
    m5_structure["Segment"].nunique()
)

print("\nПервые 5 подтверждённых экстремумов:")

print(
    m5_structure.loc[
        m5_structure["High_Confirmed"] |
        m5_structure["Low_Confirmed"],
        [
            "Confirmed_High_Price",
            "Confirmed_Low_Price"
        ]
    ].head().to_string()
)


# ===== ORIGINAL NOTEBOOK CELL 28 =====
import pandas as pd

m5_bos = m5_structure.copy()

# Последние известные подтверждённые уровни
m5_bos["Last_High"] = (
    m5_bos.groupby("Segment")["Confirmed_High_Price"]
    .ffill()
)

m5_bos["Last_Low"] = (
    m5_bos.groupby("Segment")["Confirmed_Low_Price"]
    .ffill()
)

# Уровень становится доступным после закрытия
# свечи подтверждения, поэтому сдвигаем на 1 M5
m5_bos["Known_High"] = (
    m5_bos.groupby("Segment")["Last_High"].shift(1)
)

m5_bos["Known_Low"] = (
    m5_bos.groupby("Segment")["Last_Low"].shift(1)
)

bos_results = []

for row in signals_ready.itertuples():

    # Свеча касания должна сначала закрыться
    start = row.Touch_Time + pd.Timedelta(minutes=5)
    end = start + pd.Timedelta(minutes=60)

    candles = m5_bos.loc[
        (m5_bos.index >= start) &
        (m5_bos.index < end)
    ]

    found_time = pd.NaT
    found_price = float("nan")
    found_level = float("nan")

    previous_close = None

    for candle_time, candle in candles.iterrows():

        if row.Direction == "up":
            level = candle["Known_High"]

            if (
                pd.notna(level) and
                candle["close"] > level and
                candle["open"] <= level
            ):
                found_time = candle_time
                found_price = candle["close"]
                found_level = level
                break

        else:
            level = candle["Known_Low"]

            if (
                pd.notna(level) and
                candle["close"] < level and
                candle["open"] >= level
            ):
                found_time = candle_time
                found_price = candle["close"]
                found_level = level
                break

bos_results.append({
    "BOS_Time": found_time,
    "BOS_Price": found_price,
    "BOS_Level": found_level
})

bos_df = pd.DataFrame(bos_results)

signals_bos = signals_ready.reset_index(drop=True).copy()

signals_bos = pd.concat(
    [signals_bos, bos_df],
    axis=1
)

signals_bos["BOS_Found"] = (
    signals_bos["BOS_Time"].notna()
)

print("ПОИСК BOS НА M5")
print("-------------------------")

print("Сигналов Fib 0.5:", len(signals_bos))

print(
    "Найдено BOS:",
    signals_bos["BOS_Found"].sum()
)

print(
    "Без BOS за 60 минут:",
    (~signals_bos["BOS_Found"]).sum()
)

print("\nBOS по направлениям:")
print(
    signals_bos.loc[
        signals_bos["BOS_Found"],
        "Direction"
    ].value_counts()
)

print("\nПервые 5 BOS:")
print(
    signals_bos.loc[
        signals_bos["BOS_Found"],
        [
            "Direction",
            "Touch_Time",
            "BOS_Time",
            "BOS_Price",
            "BOS_Level"
        ]
    ].head().to_string(index=False)
)


# ===== ORIGINAL NOTEBOOK CELL 29 =====
import pandas as pd

bos_results = []

for row in signals_ready.itertuples():

    start = row.Touch_Time + pd.Timedelta(minutes=5)
    end = start + pd.Timedelta(minutes=60)

    candles = m5_bos.loc[
        (m5_bos.index >= start) &
        (m5_bos.index < end)
    ]

    found_time = pd.NaT
    found_price = float("nan")
    found_level = float("nan")

    for candle_time, candle in candles.iterrows():

        if row.Direction == "up":
            level = candle["Known_High"]

            if (
                pd.notna(level)
                and candle["open"] <= level
                and candle["close"] > level
            ):
                found_time = candle_time
                found_price = candle["close"]
                found_level = level
                break

        else:
            level = candle["Known_Low"]

            if (
                pd.notna(level)
                and candle["open"] >= level
                and candle["close"] < level
            ):
                found_time = candle_time
                found_price = candle["close"]
                found_level = level
                break

    # ВАЖНО: сохраняем результат КАЖДОГО сигнала
    bos_results.append({
        "BOS_Time": found_time,
        "BOS_Price": found_price,
        "BOS_Level": found_level
    })

bos_df = pd.DataFrame(bos_results)

signals_bos = pd.concat(
    [
        signals_ready.reset_index(drop=True),
        bos_df
    ],
    axis=1
)

signals_bos["BOS_Found"] = signals_bos["BOS_Time"].notna()

# Проверяем последовательность событий
valid_bos = signals_bos["BOS_Found"]

time_errors = (
    (
        signals_bos.loc[valid_bos, "BOS_Time"]
        <= signals_bos.loc[valid_bos, "Touch_Time"]
    )
    |
    (
        signals_bos.loc[valid_bos, "BOS_Time"]
        >= signals_bos.loc[valid_bos, "Touch_Time"]
        + pd.Timedelta(minutes=65)
    )
).sum()

print("ПОВТОРНАЯ ПРОВЕРКА BOS")
print("--------------------------")

print("Исходных сигналов:", len(signals_ready))
print("Обработано результатов:", len(bos_results))
print("Найдено BOS:", signals_bos["BOS_Found"].sum())
print("Без BOS:", (~signals_bos["BOS_Found"]).sum())
print("Ошибок времени:", time_errors)

print("\nПо направлениям:")
print(
    signals_bos.loc[
        signals_bos["BOS_Found"], "Direction"
    ].value_counts()
)

print("\nПервые 5 BOS:")
print(
    signals_bos.loc[
        signals_bos["BOS_Found"],
        ["Direction", "Touch_Time", "BOS_Time", "BOS_Price", "BOS_Level"]
    ].head().to_string(index=False)
)


# ===== ORIGINAL NOTEBOOK CELL 30 =====
import pandas as pd

bos_check = signals_bos.copy()

# BOS считается подтверждённым после закрытия M5
bos_check["BOS_Confirmed_Time"] = (
    bos_check["BOS_Time"] + pd.Timedelta(minutes=5)
)

# Следующий H1-импульс не должен быть известен
# до подтверждения BOS
bos_check["Impulse_Still_Active"] = (
    bos_check["BOS_Found"] &
    (
        bos_check["Next_Impulse_Time"].isna() |
        (
            bos_check["BOS_Confirmed_Time"] <
            bos_check["Next_Impulse_Time"]
        )
    )
)

# Проверка временного порядка
bos_check["Correct_Order"] = (
    bos_check["BOS_Found"] &
    (
        bos_check["BOS_Time"] >=
        bos_check["Touch_Time"] + pd.Timedelta(minutes=5)
    ) &
    (
        bos_check["BOS_Confirmed_Time"] <=
        bos_check["Touch_Time"] + pd.Timedelta(minutes=65)
    )
)

bos_check["Valid_BOS"] = (
    bos_check["Impulse_Still_Active"] &
    bos_check["Correct_Order"]
)

bos_valid = bos_check[
    bos_check["Valid_BOS"]
].copy()

print("ПРОВЕРКА АКТУАЛЬНОСТИ BOS")
print("-----------------------------")

print("Всего найдено BOS:", bos_check["BOS_Found"].sum())

print(
    "BOS при актуальном импульсе:",
    bos_check["Impulse_Still_Active"].sum()
)

print(
    "Исключено после проверки:",
    bos_check["BOS_Found"].sum() - len(bos_valid)
)

print("Осталось BOS:", len(bos_valid))

print("\nПо направлениям:")
print(bos_valid["Direction"].value_counts())

print("\nПервые 5:")
print(
    bos_valid[
        [
            "Direction",
            "Touch_Time",
            "BOS_Time",
            "BOS_Confirmed_Time",
            "Next_Impulse_Time"
        ]
    ].head().to_string(index=False)
)


# ===== ORIGINAL NOTEBOOK CELL 31 =====
import pandas as pd

# Время подтверждения каждого экстремума M5
m5_levels = m5_bos.copy()

m5_levels["High_Confirmation_Time"] = pd.NaT
m5_levels["Low_Confirmation_Time"] = pd.NaT

high_mask = m5_levels["High_Confirmed"]
low_mask = m5_levels["Low_Confirmed"]

m5_levels.loc[
    high_mask, "High_Confirmation_Time"
] = m5_levels.index[high_mask]

m5_levels.loc[
    low_mask, "Low_Confirmation_Time"
] = m5_levels.index[low_mask]

# Последнее подтверждение внутри каждого сегмента
m5_levels["Last_High_Time"] = (
    m5_levels.groupby("Segment")["High_Confirmation_Time"]
    .ffill()
)

m5_levels["Last_Low_Time"] = (
    m5_levels.groupby("Segment")["Low_Confirmation_Time"]
    .ffill()
)

# Сдвиг на одну свечу: уровень известен после закрытия
m5_levels["Known_High_Time"] = (
    m5_levels.groupby("Segment")["Last_High_Time"]
    .shift(1)
)

m5_levels["Known_Low_Time"] = (
    m5_levels.groupby("Segment")["Last_Low_Time"]
    .shift(1)
)

results = []

for row in bos_valid.itertuples():

    candle = m5_levels.loc[row.BOS_Time]

    if row.Direction == "up":
        level_time = candle["Known_High_Time"]
        actual_level = candle["Known_High"]
    else:
        level_time = candle["Known_Low_Time"]
        actual_level = candle["Known_Low"]

    results.append({
        "BOS_Time": row.BOS_Time,
        "Level_Confirmation_Time": level_time,
        "Actual_Level": actual_level,
        "Recorded_Level": row.BOS_Level,
        "Touch_Time": row.Touch_Time
    })

level_check = pd.DataFrame(results)

level_check["Level_Before_BOS"] = (
    level_check["Level_Confirmation_Time"]
    + pd.Timedelta(minutes=5)
    <= level_check["BOS_Time"]
)

level_check["Level_Before_Touch"] = (
    level_check["Level_Confirmation_Time"]
    + pd.Timedelta(minutes=5)
    <= level_check["Touch_Time"]
)

level_check["Price_Matches"] = (
    (level_check["Actual_Level"] -
     level_check["Recorded_Level"]).abs() < 1e-9
)

print("ПРОВЕРКА УРОВНЕЙ BOS")
print("-------------------------")

print("Всего BOS:", len(level_check))

print(
    "Уровень известен до BOS:",
    level_check["Level_Before_BOS"].sum()
)

print(
    "Уровень известен до касания Fib:",
    level_check["Level_Before_Touch"].sum()
)

print(
    "Уровень подтверждён после касания Fib:",
    (
        level_check["Level_Before_BOS"] &
        ~level_check["Level_Before_Touch"]
    ).sum()
)

print(
    "Совпадение цен уровней:",
    level_check["Price_Matches"].sum()
)

print("\nПервые 5:")
print(level_check.head().to_string(index=False))


# ===== ORIGINAL NOTEBOOK CELL 33 =====
import pandas as pd

bos_strict = bos_valid.reset_index(drop=True).copy()

# Приводим все даты к единому часовому поясу UTC
level_time = pd.to_datetime(
    level_check["Level_Confirmation_Time"],
    utc=True
)

touch_time = pd.to_datetime(
    bos_strict["Touch_Time"],
    utc=True
)

bos_time = pd.to_datetime(
    bos_strict["BOS_Time"],
    utc=True
)

# Используем одинаковые индексы
level_time = level_time.reset_index(drop=True)
touch_time = touch_time.reset_index(drop=True)
bos_time = bos_time.reset_index(drop=True)

# Момент, когда информация стала доступна
level_known = level_time + pd.Timedelta(minutes=5)
touch_close = touch_time + pd.Timedelta(minutes=5)

# Строгий BOS
bos_strict["Strict_BOS"] = (
    (level_known >= touch_close) &
    (level_known <= bos_time) &
    level_check["Price_Matches"].reset_index(drop=True)
)

strict_signals = bos_strict[
    bos_strict["Strict_BOS"]
].copy()

print("СТРОГИЙ BOS ПОСЛЕ FIBONACCI")
print("-----------------------------")

print("Исходных BOS:", len(bos_strict))
print("Строгих BOS:", len(strict_signals))
print("Исключено:", len(bos_strict) - len(strict_signals))

print("\nПо направлениям:")
print(strict_signals["Direction"].value_counts())

print("\nПервые 5:")
print(
    strict_signals[
        [
            "Direction",
            "Touch_Time",
            "BOS_Time",
            "BOS_Level"
        ]
    ].head().to_string(index=False)
)


# ===== ORIGINAL NOTEBOOK CELL 34 =====
import pandas as pd

entry_check = strict_signals.copy()

# Время входа — сразу после закрытия свечи BOS
entry_check["Entry_Time"] = (
    entry_check["BOS_Time"] +
    pd.Timedelta(minutes=5)
)

# BID и ASK в британском часовом поясе
bid_entry = bid_clean.copy()
ask_entry = ask_clean.copy()

bid_entry.index = bid_entry.index.tz_convert("Europe/London")
ask_entry.index = ask_entry.index.tz_convert("Europe/London")

# Проверяем наличие следующей свечи
entry_check["BID_Open"] = (
    bid_entry["open"].reindex(
        pd.DatetimeIndex(entry_check["Entry_Time"])
    ).to_numpy()
)

entry_check["ASK_Open"] = (
    ask_entry["open"].reindex(
        pd.DatetimeIndex(entry_check["Entry_Time"])
    ).to_numpy()
)

entry_check["Entry_Available"] = (
    entry_check["BID_Open"].notna() &
    entry_check["ASK_Open"].notna()
)

# Правильная сторона котировки
entry_check["Entry_Price"] = float("nan")

buy_mask = (
    entry_check["Entry_Available"] &
    (entry_check["Direction"] == "up")
)

sell_mask = (
    entry_check["Entry_Available"] &
    (entry_check["Direction"] == "down")
)

entry_check.loc[buy_mask, "Entry_Price"] = (
    entry_check.loc[buy_mask, "ASK_Open"]
)

entry_check.loc[sell_mask, "Entry_Price"] = (
    entry_check.loc[sell_mask, "BID_Open"]
)

# Спред непосредственно на открытии свечи входа
entry_check["Entry_Spread_Pips"] = (
    entry_check["ASK_Open"] -
    entry_check["BID_Open"]
) / 0.0001

print("ПРОВЕРКА ВХОДОВ")
print("-------------------------")

print("Строгих BOS:", len(entry_check))

print(
    "Доступных входов:",
    entry_check["Entry_Available"].sum()
)

print(
    "Пропущено из-за отсутствия свечи:",
    (~entry_check["Entry_Available"]).sum()
)

print("\nСпред на входе:")
print(
    entry_check.loc[
        entry_check["Entry_Available"],
        "Entry_Spread_Pips"
    ].describe().round(2)
)

print("\nПервые 5 входов:")
print(
    entry_check[
        [
            "Direction",
            "Entry_Time",
            "Entry_Price",
            "Entry_Spread_Pips"
        ]
    ].head().to_string(index=False)
)


# ===== ORIGINAL NOTEBOOK CELL 35 =====
import pandas as pd

sl_check = entry_check[
    entry_check["Entry_Available"]
].copy()

stop_prices = []
risk_pips = []
window_counts = []

for row in sl_check.itertuples():

    start = row.Touch_Time
    end = row.BOS_Time

    bid_window = bid_entry.loc[
        (bid_entry.index >= start) &
        (bid_entry.index <= end)
    ]

    ask_window = ask_entry.loc[
        (ask_entry.index >= start) &
        (ask_entry.index <= end)
    ]

    if (
        bid_window.empty
        or ask_window.empty
        or len(bid_window) != len(ask_window)
    ):
        stop_prices.append(float("nan"))
        risk_pips.append(float("nan"))
        window_counts.append(0)
        continue

    if row.Direction == "up":
        stop = bid_window["low"].min() - 0.0001
        risk = (row.Entry_Price - stop) / 0.0001
    else:
        stop = ask_window["high"].max() + 0.0001
        risk = (stop - row.Entry_Price) / 0.0001

    stop_prices.append(stop)
    risk_pips.append(risk)
    window_counts.append(len(bid_window))

sl_check["Stop_Loss"] = stop_prices
sl_check["Risk_Pips"] = risk_pips
sl_check["Window_M5_Count"] = window_counts

sl_check["Valid_Risk"] = (
    sl_check["Risk_Pips"].notna()
    & (sl_check["Risk_Pips"] > 0)
)

print("ПРОВЕРКА STOP LOSS")
print("-------------------------")

print("Доступных входов:", len(sl_check))
print("Корректный риск:", sl_check["Valid_Risk"].sum())
print("Некорректный риск:", (~sl_check["Valid_Risk"]).sum())

print("\nРиск в пипсах:")
print(sl_check["Risk_Pips"].describe().round(2))

print("\nРиск менее 3 pip:",
      (sl_check["Risk_Pips"] < 3).sum())

print("Риск более 30 pip:",
      (sl_check["Risk_Pips"] > 30).sum())

print("\nПервые 5:")
print(
    sl_check[
        [
            "Direction",
            "Entry_Time",
            "Entry_Price",
            "Stop_Loss",
            "Risk_Pips",
            "Window_M5_Count"
        ]
    ].head().to_string(index=False)
)


# ===== ORIGINAL NOTEBOOK CELL 36 =====
import pandas as pd

trades_ready = sl_check[
    sl_check["Valid_Risk"]
].copy()

# Соотношение риск/прибыль
RR = 2.0

trades_ready["Take_Profit"] = float("nan")

# Покупки
buy = trades_ready["Direction"] == "up"

trades_ready.loc[buy, "Take_Profit"] = (
    trades_ready.loc[buy, "Entry_Price"]
    + trades_ready.loc[buy, "Risk_Pips"] * RR * 0.0001
)

# Продажи
sell = trades_ready["Direction"] == "down"

trades_ready.loc[sell, "Take_Profit"] = (
    trades_ready.loc[sell, "Entry_Price"]
    - trades_ready.loc[sell, "Risk_Pips"] * RR * 0.0001
)

# Проверка расположения уровней
correct_buy = (
    (trades_ready.loc[buy, "Stop_Loss"] <
     trades_ready.loc[buy, "Entry_Price"])
    &
    (trades_ready.loc[buy, "Take_Profit"] >
     trades_ready.loc[buy, "Entry_Price"])
)

correct_sell = (
    (trades_ready.loc[sell, "Stop_Loss"] >
     trades_ready.loc[sell, "Entry_Price"])
    &
    (trades_ready.loc[sell, "Take_Profit"] <
     trades_ready.loc[sell, "Entry_Price"])
)

print("ПРОВЕРКА TAKE PROFIT")
print("-------------------------")

print("Всего сделок:", len(trades_ready))
print("RR:", RR)

print("Корректных BUY:", correct_buy.sum(), "из", buy.sum())
print("Корректных SELL:", correct_sell.sum(), "из", sell.sum())

print("\nПервые 5 сделок:")
print(
    trades_ready[
        [
            "Direction",
            "Entry_Time",
            "Entry_Price",
            "Stop_Loss",
            "Take_Profit",
            "Risk_Pips"
        ]
    ].head().to_string(index=False)
)


# Export for cross-checking with the original research results.
out = folder / 'GBPUSD_REPRODUCED_SIGNALS.csv'
trades_ready.to_csv(out, index=False)
print('EXPORTED', out)
print('SIGNALS', len(trades_ready))
if len(trades_ready) != 32:
    print('WARNING: Expected 32 historical signals; investigate discrepancy before enabling alerts.')
