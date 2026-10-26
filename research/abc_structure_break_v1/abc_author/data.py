from pathlib import Path
import hashlib
import numpy as np
import pandas as pd

STEP = pd.Timedelta(minutes=5)

def load(bid_path, ask_path, asof=None):
    frames = []
    for path in (bid_path, ask_path):
        f = pd.read_csv(path)
        f.columns = [c.lower() for c in f.columns]
        f = f.rename(columns={f.columns[0]: 'timestamp'})
        # Require explicit timezone; never silently localize an ambiguous export.
        if not f.timestamp.astype(str).str.contains(r'(?:Z|[+-]\d{2}:?\d{2})$', regex=True).all():
            raise ValueError('Timezone must be explicit')
        f['timestamp'] = pd.to_datetime(f.timestamp, utc=True)
        if f.timestamp.duplicated().any() or not f.timestamp.is_monotonic_increasing:
            raise ValueError('Duplicate or unsorted timestamps')
        f = f.set_index('timestamp')[['open','high','low','close']].astype(float)
        if not np.isfinite(f.to_numpy()).all() or (f <= 0).any().any():
            raise ValueError('Invalid price')
        if ((f.high < f[['open','close','low']].max(axis=1)) |
            (f.low > f[['open','close','high']].min(axis=1))).any():
            raise ValueError('Invalid OHLC')
        if ((f.index.minute % 5 != 0) | (f.index.second != 0)).any():
            raise ValueError('Not M5 bar-open timestamps')
        frames.append(f)
    b,a = frames
    if not b.index.equals(a.index):
        raise ValueError('BID/ASK timestamps do not match; no silent inner join')
    if (a < b - 1e-10).any().any():
        raise ValueError('ASK below BID')
    f = b.add_prefix('bid_').join(a.add_prefix('ask_'))
    if asof is not None:
        t = pd.Timestamp(asof)
        if t.tzinfo is None:
            raise ValueError('asof must include timezone')
        f = f[f.index + STEP <= t]
    f['segment'] = (f.index.to_series().diff() != STEP).cumsum().to_numpy()
    return f

def bid_bars(f):
    return f[['bid_open','bid_high','bid_low','bid_close','segment']].rename(columns=lambda c:c.replace('bid_',''))

def hourly(f):
    b = bid_bars(f)
    g = b.resample('1h', label='left', closed='left')
    h = g.agg({'open':'first','high':'max','low':'min','close':'last','segment':'first'})
    good = (g.close.count() == 12) & (g.segment.nunique() == 1)
    h = h[good].copy()
    h['segment'] = (h.index.to_series().diff() != pd.Timedelta(hours=1)).cumsum().to_numpy()
    return h

def manifest(bid,ask,f):
    return {'files': {str(p): hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in (bid,ask)},
            'rows':len(f),'first_open':str(f.index[0]),'last_open':str(f.index[-1]),
            'gap_count':int((f.index.to_series().diff().dropna()!=STEP).sum())}
