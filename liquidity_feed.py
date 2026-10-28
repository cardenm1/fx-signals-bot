"""Bounded Dukascopy chart tick reader; no SDK imports or runtime patches."""
import json
import time
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

PAIRS = {p: p[:3] + '/' + p[3:] for p in (
    'GBPUSD', 'EURUSD', 'USDJPY', 'USDCHF', 'USDCAD', 'AUDUSD',
    'NZDUSD', 'GBPJPY', 'EURJPY', 'EURGBP', 'NZDCHF')}
URL = 'https://freeserv.dukascopy.com/2.0/index.php'


def fetch_m1(pair, *, now=None, session=None):
    now = pd.Timestamp(now or datetime.now(timezone.utc))
    start = (now - timedelta(hours=4)).ceil('min')
    cursor = int(start.timestamp() * 1000)
    end = int(now.timestamp() * 1000)
    own = session is None
    session = session or requests.Session()
    if own:
        session.mount('https://', HTTPAdapter(max_retries=Retry(
            total=2, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504],
            respect_retry_after_header=False)))
    rows = []
    deadline = time.monotonic() + 90
    try:
        for _ in range(60):
            if time.monotonic() >= deadline:
                raise TimeoutError('Dukascopy collection deadline')
            response = session.get(URL, params={
                'path': 'chart/json3', 'instrument': PAIRS[pair],
                'interval': 'TICK', 'offer_side': 'B', 'time_direction': 'N',
                'last_update': cursor, 'limit': 30000, 'jsonp': 'liquidity'},
                headers={'User-Agent': 'Mozilla/5.0',
                         'Referer': 'https://freeserv.dukascopy.com/2.0/'},
                timeout=(5, 15))
            response.raise_for_status()
            text = response.text
            if not text.startswith('liquidity(') or not text.endswith(');'):
                raise ValueError('Unexpected Dukascopy response')
            batch = json.loads(text[len('liquidity('):-2])
            if not isinstance(batch, list):
                raise ValueError('Invalid tick response')
            if not batch:
                break
            values = np.asarray(batch, dtype=float)
            if values.ndim != 2 or values.shape[1] != 5:
                raise ValueError('Invalid tick schema')
            if (not np.isfinite(values).all() or (values[:, 1:3] <= 0).any()
                    or (values[:, 2] < values[:, 1]).any()
                    or (np.diff(values[:, 0]) < 0).any()
                    or values[0, 0] < cursor):
                raise ValueError('Invalid or unordered quotes')
            rows.extend(row for row in batch if row[0] <= end)
            latest = int(values[-1, 0])
            if latest >= end or latest == cursor or len(batch) < 30000:
                break
            # Inclusive pagination. Overlap is harmless for OHLC and retained.
            cursor = latest
        else:
            raise TimeoutError('Dukascopy pagination limit')
    finally:
        if own:
            session.close()
    if not rows:
        raise ValueError('No market ticks')
    ticks = pd.Series([r[1] for r in rows],
                      index=pd.to_datetime([r[0] for r in rows], unit='ms', utc=True))
    bars = ticks.resample('min').ohlc().dropna()
    # A later tick proves the preceding minute is closed and fully retrieved.
    cutoff = min(now.floor('min'), ticks.index[-1].floor('min'))
    return bars[(bars.index >= start) & (bars.index < cutoff)]


if __name__ == '__main__':
    for pair in PAIRS:
        frame = fetch_m1(pair)
        if frame.empty:
            raise RuntimeError(f'{pair}: empty data')
        age = (pd.Timestamp.now(tz='UTC') - frame.index[-1]).total_seconds()
        print(pair, PAIRS[pair], len(frame), str(frame.index[-1]),
              'age_seconds=', round(age), 'close=', frame.iloc[-1]['close'], flush=True)
        if age > 600:
            raise RuntimeError(f'{pair}: stale data (market may be closed)')
