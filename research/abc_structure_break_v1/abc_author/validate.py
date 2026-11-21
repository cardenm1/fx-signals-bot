"""Audit saved signal chronology and paired-exit comparison, no messaging."""
import json
from pathlib import Path
import pandas as pd
from .data import load,STEP
from .engine import signals,Config,aggregate,engulf,POINT
from .backtest import execute
from .research import metrics


def main():
    root=Path('abc_author_results')
    f=load('abc_author_data/GBPUSD_BID_M5_CLEAN.csv','abc_author_data/GBPUSD_ASK_M5_CLEAN.csv')
    ss=pd.read_csv(root/'signals.csv').to_dict('records')
    bars={cfg:aggregate(f,cfg) for cfg in (5,15)}
    for s in ss:
        side=1 if s['direction']=='long' else -1
        low=5 if s['pair']=='H1/M5' else 15
        l=bars[low];known=pd.Timestamp(s['signal_time']);bar=l.loc[pd.Timestamp(s['signal_bar_open'])]
        prev=l.loc[pd.Timestamp(s['engulf_previous'])]
        assert pd.Timestamp(s['context_known'])<=pd.Timestamp(s['touch_time'])
        assert pd.Timestamp(s['touch_known'])<=pd.Timestamp(s['p1_time'])
        assert pd.Timestamp(s['p4_known'])==pd.Timestamp(s['pattern_known'])<=pd.Timestamp(s['engulf_previous'])
        assert pd.Timestamp(s['target5_frozen'])==pd.Timestamp(s['touch_known'])
        assert pd.Timestamp(s['signal_bar_open'])+pd.Timedelta(minutes=low)==known
        for n in range(1,5):
            assert pd.Timestamp(s[f'p{n}_time'])<pd.Timestamp(s[f'p{n}_known'])<=known
        assert all(pd.Timestamp(s[f'p{n}_time'])<pd.Timestamp(s[f'p{n+1}_time']) for n in range(1,4))
        assert (s['p3_price']-s['p1_price'])*side>=POINT-1e-10
        assert (s['p4_price']-s['p2_price'])*side>=POINT-1e-10
        assert engulf(prev,bar,side)
    (root/'signal_validation.json').write_text(json.dumps(dict(signals_checked=len(ss),passed=True),indent=2))
    stress={};matched={}
    for pair in ('H1/M5','H4/M15'):
        subset=[s for s in ss if s['pair']==pair]
        stress[pair]={}
        for mode in ('2R','point5'):
            stress[pair][mode]=[]
            for slip,fee in [(0,0),(.2,0),(.5,0),(.2,.5),(.5,.5)]:
                ts=[execute(f,s,slippage_pips=slip,fee_pips=fee,target_mode=mode) for s in subset]
                stress[pair][mode].append(dict(slippage_pips=slip,fee_pips=fee,**metrics(ts)))
        ts={mode:[execute(f,s,target_mode=mode) for s in subset] for mode in ('2R','point5')}
        common=set(t['signal_id'] for t in ts['2R'] if 'r_multiple' in t)&set(t['signal_id'] for t in ts['point5'] if 'r_multiple' in t)
        matched[pair]={mode:{d:metrics([t for t in ts[mode] if t['signal_id'] in common and (d=='ALL' or t['direction']==d)]) for d in ('ALL','long','short')} for mode in ts}
    (root/'cost_sensitivity.json').write_text(json.dumps(stress,indent=2))
    (root/'matched_exit_comparison.json').write_text(json.dumps(matched,indent=2))
    print(f'{len(ss)} signal chronologies validated; costs and matched exits saved.')

if __name__=='__main__':main()
