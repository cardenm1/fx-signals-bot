"""Optional read-only Dukascopy tick adapter. Imported only on approved CLI run."""
import hashlib,importlib.metadata,json,multiprocessing,queue,os
from pathlib import Path
import pandas as pd
from .engine import BAR,utc,validate_pair

def _worker(result,start,end,interval,side):
    try:
        if importlib.metadata.version('dukascopy-python')!='4.0.1':raise ValueError('Require pinned dukascopy-python==4.0.1')
        import dukascopy_python as sdk
        from dukascopy_python.instruments import INSTRUMENT_FX_MAJORS_GBP_USD
        data=sdk.fetch(instrument=INSTRUMENT_FX_MAJORS_GBP_USD,interval=getattr(sdk,interval),offer_side=getattr(sdk,side),start=utc(start).to_pydatetime(),end=utc(end).to_pydatetime(),max_retries=2)
        result.put(('ok',data))
    except Exception as exc:result.put(('error',type(exc).__name__+': '+str(exc)))

def fetch_bounded(start,end,interval='INTERVAL_TICK',side='OFFER_SIDE_BID',timeout=30):
    ctx=multiprocessing.get_context('spawn');result=ctx.Queue();child=ctx.Process(target=_worker,args=(result,str(start),str(end),interval,side));child.start()
    try:
        try:kind,data=result.get(timeout=timeout)
        except queue.Empty:raise TimeoutError('Dukascopy request deadline exceeded')
        if kind!='ok':raise RuntimeError(data)
        return data
    finally:
        if child.is_alive():child.terminate()
        child.join(timeout=5);result.close()

def from_ticks(raw,now,finality_seconds=2,*,request_start=None,request_end=None):
    if not isinstance(raw.index,pd.DatetimeIndex) or raw.index.tz is None:raise ValueError('SDK tick timestamp lost UTC timezone')
    data=raw.rename(columns={'bidPrice':'bid','askPrice':'ask'})[['bid','ask']].astype(float)
    if not data.index.is_monotonic_increasing:raise ValueError('SDK returned nonmonotonic ticks')
    if len(data) and data.index[-1]>utc(now):raise ValueError('SDK returned future ticks')
    data.index=data.index.tz_convert('UTC')
    if request_start is None or request_end is None:
        raise ValueError('Explicit successful request coverage required')
    start,end=utc(request_start),utc(request_end)
    if start>end or end>utc(now):raise ValueError('Invalid UTC request coverage')
    if len(data) and ((data.index<start)|(data.index>end)).any():raise ValueError('Ticks outside request coverage')
    import numpy as np
    if not np.isfinite(data.to_numpy()).all() or (data<=0).any().any() or (data.ask<data.bid).any():raise ValueError('Invalid tick quotes')
    # Coverage comes from the successful bounded request, never first/last tick.
    # Empty minutes cannot prove missing ticks; no synthetic bars are filled.
    first_complete=start.ceil('5min')
    last_close=min(end,utc(now)-pd.Timedelta(seconds=finality_seconds))
    # Preserve all quote changes including ticks sharing a millisecond.
    pairs=[]
    for col in ['bid','ask']:
        bars=data[col].resample('5min',closed='left',label='left').ohlc().dropna()
        bars=bars[(bars.index>=first_complete)&(bars.index+BAR<=last_close)];pairs.append(bars)
    validate_pair(*pairs);return *pairs,data

def archive_raw(root,raw,start,end):
    text=raw.to_csv(index_label='timestamp');digest=hashlib.sha256(text.encode()).hexdigest();folder=Path(root)/'raw';folder.mkdir(exist_ok=True)
    path=folder/(digest+'.csv')
    if not path.exists():
        with path.open('x') as stream:stream.write(text);stream.flush();os.fsync(stream.fileno())
    elif path.read_text()!=text:raise ValueError('Raw content hash collision/change')
    return {'path':'raw/'+path.name,'sha256':digest,'request_start_utc':str(start),'request_end_utc':str(end),'rows':len(raw),'source':'dukascopy-python4.0.1 tick endpoint'}
