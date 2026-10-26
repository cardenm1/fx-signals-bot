import argparse
import json
from pathlib import Path
from collections import Counter
import pandas as pd
from .data import load,manifest,STEP
from .engine import signals,Config,aggregate
from .backtest import execute


def metrics(trades):
    closed=[t for t in trades if 'r_multiple' in t]
    equity=peak=dd=0.
    for t in sorted(closed,key=lambda t:(t['exit_time'],t['signal_id'])):
        equity+=t['r_multiple'];peak=max(peak,equity);dd=max(dd,peak-equity)
    return dict(signals=len(trades),trades=len(closed),wins=sum(t['status']=='win' for t in closed),
                win_rate_pct=100*sum(t['status']=='win' for t in closed)/len(closed) if closed else None,
                total_r=equity,max_drawdown_r=dd,statuses=dict(Counter(t['status'] for t in trades)))


def run(bid,ask,out,audit=False,slippage=.2,fee=0.):
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    f=load(bid,ask,asof=pd.Timestamp.now(tz='UTC'))
    if f.empty:raise ValueError('Empty closed history')
    summary=dict(data=manifest(bid,ask,f),execution=dict(slippage_pips=slippage,round_trip_fee_pips=fee),pairs={})
    all_signals=[];all_trades=[];all_events=[];checks=[]
    for cfg in (Config(),Config(high_minutes=240,low_minutes=15)):
        pair=f'H{cfg.high_minutes//60}/M{cfg.low_minutes}'
        ss,ev=signals(f,cfg,with_events=True)
        print(pair,len(ss),'signals',flush=True)
        result=dict(config=cfg.__dict__,htf_bars=len(aggregate(f,cfg.high_minutes)),ltf_bars=len(aggregate(f,cfg.low_minutes)),
                    funnel=dict(Counter(e['event'] for e in ev)),targets={})
        for mode in ('2R','point5'):
            ts=[dict(execute(f,s,slippage_pips=slippage,fee_pips=fee,target_mode=mode),target_mode=mode) for s in ss]
            result['targets'][mode]={d:metrics([t for t in ts if d=='ALL' or t['direction']==d]) for d in ('ALL','long','short')}
            all_trades+=ts
        if audit:
            # One cut at every signal close, plus deterministic non-signal cuts.
            cuts=set(range(1000,len(f),5000))|{f.index.get_loc(pd.Timestamp(s['signal_time'])-STEP)+1 for s in ss}
            for n in sorted(cuts):
                end=f.index[n-1]+STEP
                expected=[s for s in ss if pd.Timestamp(s['signal_time'])<=end]
                actual=signals(f.iloc[:n],cfg)
                if actual!=expected:raise AssertionError(f'Prefix mismatch {pair} rows={n}')
                checks.append(dict(pair=pair,rows=n,signals=len(actual),passed=True))
        summary['pairs'][pair]=result;all_signals+=ss;all_events+=ev
    pd.DataFrame(all_signals).to_csv(out/'signals.csv',index=False)
    pd.DataFrame(all_trades).to_csv(out/'trades.csv',index=False)
    pd.DataFrame(all_events).to_csv(out/'events.csv',index=False)
    if audit:
        summary['prefix_audit']=dict(checks=len(checks),passed=True)
        (out/'prefix_audit.json').write_text(json.dumps(checks,indent=2))
    summary['note']='Independent overlapping unit-risk trades; closed-R drawdown, not account or mark-to-market equity. Two exits are separate experiments, not added together. BUY is mirrored assumption.'
    (out/'summary.json').write_text(json.dumps(summary,indent=2))
    print(json.dumps(summary,indent=2))
    return summary


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--bid',default='abc_author_data/GBPUSD_BID_M5_CLEAN.csv')
    p.add_argument('--ask',default='abc_author_data/GBPUSD_ASK_M5_CLEAN.csv')
    p.add_argument('--out',default='abc_author_results')
    p.add_argument('--audit',action='store_true')
    p.add_argument('--slippage-pips',type=float,default=.2)
    p.add_argument('--fee-pips',type=float,default=0.)
    a=p.parse_args();run(a.bid,a.ask,a.out,a.audit,a.slippage_pips,a.fee_pips)

if __name__=='__main__':main()
