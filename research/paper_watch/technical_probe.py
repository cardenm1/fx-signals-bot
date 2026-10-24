"""Bounded read-only test, ENTRY/EXIT forbidden even in the diagnostic journal."""
import hashlib,importlib.metadata,json,time
from pathlib import Path
import pandas as pd
from .feed import fetch_bounded,from_ticks
from .engine import BAR,validate_pair
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT.parent/'gbpusd-paper-watch-technical-test';OUT.mkdir(exist_ok=True)

def market_open(t):
    ny=pd.Timestamp(t).tz_convert('America/New_York');return ny.weekday()<4 or (ny.weekday()==4 and ny.hour<17) or (ny.weekday()==6 and ny.hour>=17)

def run():
    now=pd.Timestamp.now(tz='UTC');results={'started_utc':str(now),'market_open_expected':market_open(now),'execution_forbidden':True,'continuous_monitoring':False,'requests':[],'versions':{k:importlib.metadata.version(k) for k in ['dukascopy-python','requests','pandas','numpy']}}
    specs=[('bridge_bid',pd.Timestamp('2026-10-21T00:00Z'),now,'INTERVAL_MIN_5','OFFER_SIDE_BID'),('bridge_ask',pd.Timestamp('2026-10-21T00:00Z'),now,'INTERVAL_MIN_5','OFFER_SIDE_ASK'),('last_session_ticks',now-pd.Timedelta(hours=2),now,'INTERVAL_TICK','OFFER_SIDE_BID'),('closed_market_ticks',now-pd.Timedelta(minutes=3),now,'INTERVAL_TICK','OFFER_SIDE_BID')]
    for name,start,end,interval,side in specs:
        begin=time.monotonic();row={'name':name,'request_start':str(start),'request_end':str(end),'interval':interval,'side':side}
        try:
            df=fetch_bounded(start,end,interval=interval,side=side,timeout=25);received=pd.Timestamp.now(tz='UTC')
            df.to_csv(OUT/(name+'.csv'),index_label='timestamp')
            row.update({'status':'OK','rows':len(df),'received_utc':str(received),'columns':list(df.columns),'tz':str(df.index.tz),'first':str(df.index[0]) if len(df) else None,'last':str(df.index[-1]) if len(df) else None,'sha256':hashlib.sha256((OUT/(name+'.csv')).read_bytes()).hexdigest()})
            if interval=='INTERVAL_TICK' and len(df):
                b,a,t=from_ticks(df,received,request_start=start,request_end=end);b.to_csv(OUT/(name+'_M5_BID.csv'));a.to_csv(OUT/(name+'_M5_ASK.csv'))
                row.update({'closed_M5':len(b),'last_closed_open_utc':str(b.index[-1]) if len(b) else None,'last_tick_age_seconds':(received-df.index[-1]).total_seconds(),'future_ticks':int((df.index>received).sum()),'BID_ASK_crosses':int((t.ask<t.bid).sum()),'all_M5_final':bool((b.index+BAR<=received-pd.Timedelta(seconds=2)).all())})
        except Exception as exc:row.update({'status':'ERROR','error':type(exc).__name__+': '+str(exc)})
        row['elapsed_seconds']=time.monotonic()-begin;results['requests'].append(row)
        (OUT/'network_results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2));print(json.dumps(row,ensure_ascii=False),flush=True)
    results['finished_utc']=str(pd.Timestamp.now(tz='UTC'));(OUT/'network_results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
if __name__=='__main__':run()
