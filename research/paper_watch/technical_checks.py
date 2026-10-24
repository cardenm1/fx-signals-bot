"""Bounded CSV/state/restart probes. Hard forbid ENTRY/EXIT at journal boundary."""
import json,hashlib,tempfile
from pathlib import Path
import numpy as np
import pandas as pd
from .ledger import Ledger,IntegrityError
from .engine import Engine,initialize,read_pair,BAR,ts
from .feed import from_ticks
from .cli import poll
from . import model
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT.parent/'gbpusd-paper-watch-technical-test'

class TradeForbidden(RuntimeError):pass
class DiagnosticLedger(Ledger):
    def append(self,kind,*args,**kwargs):
        if kind in ['ENTRY','EXIT']:raise TradeForbidden('Technical test forbids even virtual trades')
        return super().append(kind,*args,**kwargs)
class DiagnosticEngine(Engine):
    def ticks(self,ticks,now):
        if self.position is not None:raise TradeForbidden('No existing position permitted')
        for s in list(self.pending.values()):self.reject(s,'TECHNICAL_TEST_EXECUTION_DISABLED',now)
        return super().ticks(ticks,now)

def count(j,kind):return sum(r['kind']==kind for r in j.events)
def state(b,a,now):
    d,snaps,h,levels,signals,diagnostics=model.analyse(b,a)
    closed=[s for s in snaps if s['known_at']<=now]
    validh=h[h.valid&(h.index+pd.Timedelta(hours=1)<=now)]
    return {'M5_count':len(b),'latest_M5_open':str(b.index[-1]),'H1_complete_count':len(validh),'last_closed_H1_open':str(validh.index[-1]),'D1_grid_valid_count':int(d.valid.sum()),'last_closed_D1_known':str(closed[-1]['known_at']),'current_D1_known_at':str(d.index[-1]+pd.Timedelta(days=1)),'current_D1_available':bool(d.index[-1]+pd.Timedelta(days=1)<=now),'current_D1_rows':int(d.actual.iloc[-1]),'current_D1_grid_valid':bool(d.valid.iloc[-1]),'recognized_historical_signals':len(signals),'D1_structure_closed':closed[-1]['structure']}

def run():
    now=pd.Timestamp.now(tz='UTC');oldb,olda=read_pair(ROOT/'research/data/GBPUSD_BID_M5_CLEAN.csv',ROOT/'research/data/GBPUSD_ASK_M5_CLEAN.csv');nb,na=read_pair(OUT/'bridge_bid.csv',OUT/'bridge_ask.csv')
    b=pd.concat([oldb,nb]);a=pd.concat([olda,na]);raw=pd.read_csv(OUT/'last_session_ticks.csv',index_col=0);raw.index=pd.to_datetime(raw.index,utc=True,format="ISO8601")
    latest=nb.index[-1];root=OUT/'guarded_session'
    config=initialize(root,b.iloc[:-1],a.iloc[:-1],now)
    findings={};before=state(b.iloc[:-1],a.iloc[:-1],now);after=state(b,a,now)
    with DiagnosticLedger(root) as j:
        e=DiagnosticEngine(j);initial_last=e.b.index[-1]
        def cached(start,end):return raw[(raw.index>=start)&(raw.index<=end)]
        poll(e,j,clock=lambda:now,fetcher=cached)
        findings['state_updated_from_new_source']=e.b.index[-1]==latest and initial_last<latest
        findings['last_M5_matches_source']=np.allclose(e.b.loc[latest].to_numpy(),b.loc[latest].to_numpy(),atol=1e-12,rtol=0)
        findings['new_bar_events_first_poll']=count(j,'BAR')
        e.emit('HEARTBEAT','state_before',now,now,{'technical_test':'D1_H1_M5_BEFORE','snapshot':before})
        e.emit('HEARTBEAT','state_after',now,now,{'technical_test':'D1_H1_M5_AFTER','snapshot':after})
        first_counts={kind:count(j,kind) for kind in ['BAR','SIGNAL','TICK_CURSOR','ENTRY','EXIT']}
        prefixes={p.name:p.read_bytes() for p in root.glob('*.csv')}
    with DiagnosticLedger(root) as j:
        e=DiagnosticEngine(j);findings['restart_recovers_identical_bars']=e.b.equals(b) and e.a.equals(a)
        later=now+pd.Timedelta(seconds=1);poll(e,j,clock=lambda:later,fetcher=cached)
        findings['duplicate_guard_counts_before']=first_counts
        findings['duplicate_guard_counts_after']={kind:count(j,kind) for kind in first_counts}
        findings['old_journal_prefixes_preserved']=all((root/name).read_bytes().startswith(prefix) for name,prefix in prefixes.items())
        findings['no_historical_signal_journaled']=count(j,'SIGNAL')==0
        findings['no_virtual_deals']=count(j,'ENTRY')==count(j,'EXIT')==0 and e.position is None
        try:j.append('ENTRY','forbidden',ts(later),ts(later),{})
        except TradeForbidden:findings['hard_execution_guard']=True
        empty=raw.iloc[:0]
        poll(e,j,clock=lambda:later+pd.Timedelta(seconds=1),fetcher=lambda *args:empty)
        def fail(*args):raise ConnectionError('CONTROLLED_TEST_CONNECTION_FAILURE')
        poll(e,j,clock=lambda:later+pd.Timedelta(seconds=2),fetcher=fail)
        findings['empty_data_and_connection_errors_recorded']=[json.loads(r['payload_json'])['reason'] for r in j.events if r['kind']=='ERROR']
        findings['no_ENTRY_EXIT_after_failure_tests']=count(j,'ENTRY')==count(j,'EXIT')==0
    # Independent M5 source comparison; identify partial leading request window.
    tb,ta,t=from_ticks(raw,now,request_start=raw.index[0],request_end=raw.index[-1])
    compare=[]
    for side,ticks_frame,source in [('BID',tb,nb),('ASK',ta,na)]:
        for stamp,row in ticks_frame.iterrows():
            if stamp in source.index:
                delta=np.rint((row-source.loc[stamp])*100000).astype(int)
                compare.append({'side':side,'bar_open_utc':str(stamp),'leading_partial':bool(stamp<raw.index[0]),'all_OHLC_match':bool((delta==0).all()),**{k+'_delta_points':int(delta[k]) for k in delta.index}})
    pd.DataFrame(compare).to_csv(OUT/'ticks_vs_M5.csv',index=False)
    findings['source_M5_comparison']={'full_bar_pairs':sum(not x['leading_partial'] for x in compare),'full_bar_mismatches':sum(not x['all_OHLC_match'] and not x['leading_partial'] for x in compare),'partial_bar_pairs':sum(x['leading_partial'] for x in compare),'partial_bar_mismatches':sum(not x['all_OHLC_match'] and x['leading_partial'] for x in compare)}
    # Deliberate interruption after durable BAR but before model analysis.
    # No trade is created; this tests an untested recovery boundary.
    bugroot=OUT/'interruption_probe';initialize(bugroot,b.iloc[:-1],a.iloc[:-1],now);calls=[]
    with DiagnosticLedger(bugroot) as j:
        def broken(b,a):calls.append('failed');raise RuntimeError('CONTROLLED_ANALYSIS_INTERRUPTION')
        e=DiagnosticEngine(j,model_fn=broken)
        poll(e,j,clock=lambda:now,fetcher=cached)
        bars_committed=count(j,'BAR')
    with DiagnosticLedger(bugroot) as j:
        def restored(b,a):calls.append('resumed');return model.analyse(b,a)
        e=DiagnosticEngine(j,model_fn=restored);poll(e,j,clock=lambda:now+pd.Timedelta(seconds=1),fetcher=cached)
        findings['interruption_after_BAR']={'BAR_committed':bars_committed,'model_calls':calls,'analysis_retried_after_restart':'resumed' in calls,'no_virtual_deals':count(j,'ENTRY')==count(j,'EXIT')==0}
    # The finality guard rejects a supplied full OHLC for a still-open bar.
    with DiagnosticLedger(root) as j:
        e=DiagnosticEngine(j);open_bar=b.iloc[-1:].copy();open_bar.index=pd.DatetimeIndex([now.floor('5min')]);open_ask=open_bar+.0001
        try:e.ingest(open_bar,open_ask,None,now)
        except ValueError as exc:findings['unfinished_bar_rejected']=str(exc)
    findings.update({'before':before,'after':after,'session_start':config['evaluation_start_utc'],'actual_observed_utc':str(now),'diagnostic_only':True,'network_polling_in_harness':False,'no_live_or_virtual_trades_opened':True})
    (OUT/'technical_results.json').write_text(json.dumps(findings,ensure_ascii=False,indent=2));print(json.dumps(findings,ensure_ascii=False,indent=2))
if __name__=='__main__':run()
