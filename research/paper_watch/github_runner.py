"""GBP/USD Strategy v1.0: bounded, signal-only hosted runner; no order API."""
import argparse,hashlib,json,sys
from pathlib import Path
import pandas as pd
from .engine import Engine,BAR,utc,ts,initialize,read_pair
from .ledger import Ledger,IntegrityError
from .feed import fetch_bounded,from_ticks,archive_raw
from .market import is_open,expected_closed
from . import model

VERSION='GBPUSD-Strategy-v1.0'
MAX_BATCHES=12
CHUNK=pd.Timedelta(hours=6)

def clock():return pd.Timestamp.now(tz='UTC')
def release_hash():
    folder=Path(__file__).parent
    names=['__init__.py','engine.py','model.py','ledger.py','feed.py','market.py','cli.py','github_runner.py','state_store.py','telegram_outbox.py']
    return hashlib.sha256(json.dumps({n:hashlib.sha256((folder/n).read_bytes()).hexdigest() for n in names},sort_keys=True).encode()).hexdigest()

class SignalLedger(Ledger):
    def append(self,kind,*a,**kw):
        if kind in {'ENTRY','EXIT'}:raise IntegrityError('Strategy v1.0 is SIGNAL_ONLY: execution forbidden')
        return super().append(kind,*a,**kw)

class SignalEngine(Engine):
    def ticks(self,quotes,now):
        if self.position:raise IntegrityError('Unexpected existing position')
        for signal in list(self.pending.values()):self.reject(signal,'SIGNAL_ONLY_NO_EXECUTION',now)
        return super().ticks(quotes,now)

def seed(root,b,a,now):
    root=Path(root)
    if root.exists() and any(root.iterdir()):raise ValueError('Fresh empty state directory required')
    root.mkdir(parents=True,exist_ok=True)
    initialize(root/'session',b,a,now)
    data={'version':VERSION,'mode':'SIGNAL_ONLY','created_utc':ts(now),'release_hash':release_hash(),'frozen_model':model.check_model(),'telegram_default':False}
    (root/'deployment.json').write_text(json.dumps(data,indent=2))
    return data

def verify(root):
    data=json.loads((Path(root)/'deployment.json').read_text())
    if data['version']!=VERSION or data['mode']!='SIGNAL_ONLY' or data['release_hash']!=release_hash() or data['frozen_model']!=model.check_model():raise IntegrityError('Deployment mismatch: explicit new version/session required')
    return data

def _consume(e,j,start,end,received,raw):
    if raw is None or raw.empty:
        if expected_closed(e.b.index[-1]+BAR,end):
            e.emit('MARKET_CLOSED',ts(end),end,received,{'reason':'EXPECTED_WEEKEND_NY_17'})
            return 'MARKET_CLOSED'
        e.error('EMPTY_TICK_RESPONSE_ACTIVE_INTERVAL',received);return 'DATA_UNAVAILABLE'
    archive=archive_raw(j.root,raw,start,end)
    e.emit('SOURCE_BATCH',archive['sha256']+'|'+ts(end),received,received,archive)
    b,a,quotes=from_ticks(raw,received,e.config['bar_finality_seconds'],request_start=start,request_end=end)
    next_open=e.b.index[-1]+BAR
    cutoff=min(utc(end),utc(received)-pd.Timedelta(seconds=e.config['bar_finality_seconds']))
    grid=pd.date_range(next_open,cutoff,freq='5min',inclusive='left') if next_open<cutoff else []
    missing=[t for t in grid if t+BAR<=cutoff and is_open(t) and t not in b.index]
    if missing:
        # Keep the complete prefix; do not advance past an unresolved active gap.
        limit=missing[0];b=b[b.index<limit];a=a[a.index<limit]
        quotes=quotes[quotes.index<limit]
    e.ingest(b,a,quotes,received)
    if e.halted:return 'HALTED'
    for signal in list(e.pending.values()):e.reject(signal,'SIGNAL_ONLY_NO_EXECUTION',received)
    if missing:
        e.emit('DATA_GAP','hosted|'+ts(missing[0]),missing[0],received,{'reason':'MISSING_ACTIVE_M5_RETRY_FROM_CURSOR','first_missing_utc':ts(missing[0])})
        e.error('ACTIVE_M5_GAP_RETRY_REQUIRED',received);return 'DATA_GAP'
    return 'OK'

def observe(root,fetcher=fetch_bounded,now_fn=clock,model_fn=None):
    root=Path(root);verify(root)
    status='OK';batches=0
    with SignalLedger(root/'session') as j:
        e=SignalEngine(j,model_fn)
        if e.position:raise IntegrityError('Existing position refused')
        if e.halted:raise IntegrityError('HALTED session: investigate, never auto-reset')
        initial=e.b.index[-1];initial_processed=e.last_processed
        try:
            if e.b.index[-1]>e.last_processed:e.ingest(e.b.iloc[:0],e.a.iloc[:0],None,now_fn())
            for _ in range(MAX_BATCHES):
                now=utc(now_fn());start=e.b.index[-1];end=min(now,start+CHUNK)
                # No synthetic success: report scheduled closure explicitly.
                if expected_closed(start+BAR,now):
                    e.emit('MARKET_CLOSED',ts(now),now,now,{'reason':'EXPECTED_WEEKEND_NY_17'});status='MARKET_CLOSED';break
                # Skip only scheduled weekend intervals, never missing active M5.
                if not is_open(start+BAR):
                    grid=pd.date_range(start+BAR,now,freq='5min',inclusive='left')
                    active=[t for t in grid if is_open(t)]
                    if active:start=active[0];end=min(now,start+CHUNK)
                if end<=start+BAR+pd.Timedelta(seconds=2):break
                raw=fetcher(start,end);received=utc(now_fn());batches+=1
                before=e.b.index[-1]
                status=_consume(e,j,start,end,received,raw)
                if status!='OK' or e.b.index[-1]==before:break
                if e.b.index[-1]+2*BAR>now:break
            else:status='CATCHUP_PENDING'
        except IntegrityError:raise
        except Exception as exc:
            # SDK exception text may contain credentials/URLs: store type only.
            e.error('SOURCE_OR_ANALYSIS_FAILURE:'+type(exc).__name__,now_fn());status='ERROR'
        current=utc(now_fn())
        summary={'version':VERSION,'status':status,'observed_utc':ts(current),'initial_M5_open':ts(initial),'last_M5_open':ts(e.b.index[-1]),'initial_processed_M5':ts(initial_processed),'last_processed_M5':ts(e.last_processed),'batches':batches,'signal_count':sum(r['kind']=='SIGNAL' for r in j.events),'execution':'FORBIDDEN','publication':'SEPARATE_DISABLED_BY_DEFAULT'}
        e.emit('HEARTBEAT','hosted|'+ts(current),current,current,summary)
    (root/'status.json').write_text(json.dumps(summary,indent=2))
    return summary

def main():
    p=argparse.ArgumentParser(description=VERSION+' SIGNAL_ONLY');sub=p.add_subparsers(dest='command',required=True)
    q=sub.add_parser('seed');q.add_argument('--root',required=True);q.add_argument('--bid',required=True);q.add_argument('--ask',required=True)
    q=sub.add_parser('once');q.add_argument('--root',required=True)
    a=p.parse_args()
    if a.command=='seed':
        b,c=read_pair(a.bid,a.ask);seed(a.root,b,c,clock());print('Seed prepared locally; no signal evaluation or publication.');return
    summary=observe(a.root);print(json.dumps(summary,indent=2))
    if summary['status'] not in {'OK','MARKET_CLOSED','CATCHUP_PENDING'}:sys.exit(1)
if __name__=='__main__':main()
