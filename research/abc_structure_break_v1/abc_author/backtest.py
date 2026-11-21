"""Independent trades: executable BID/ASK, next-open entry, separate 2R / frozen point-5 targets."""
import pandas as pd
from .data import STEP
from .engine import PIP


def execute(f, sig, max_bars=288, slippage_pips=0.2, fee_pips=0.0, target_mode='2R'):
    t=pd.Timestamp(sig['signal_time']); side=1 if sig['direction']=='long' else -1
    result=dict(sig)
    if t not in f.index:
        return dict(result,status='no_next_bar',pnl_pips=None)
    start=f.index.get_loc(t); r=f.iloc[start]
    if slippage_pips<0 or fee_pips<0: raise ValueError('Costs must be nonnegative')
    slip=slippage_pips*PIP
    entry=float(r.ask_open if side==1 else r.bid_open)+side*slip
    stop=float(sig['stop']); risk=(entry-stop)*side
    if risk<=0:
        return dict(result,status='invalid_entry',pnl_pips=None,entry=entry)
    if target_mode not in ('2R','point5'): raise ValueError('Invalid target mode')
    target=entry+side*2*risk if target_mode=='2R' else float(sig['target5'])
    if (target-entry)*side<=0:
        return dict(result,status='invalid_target',pnl_pips=None,entry=entry,target=target)
    result['target_mode']=target_mode
    result.update(slippage_pips=slippage_pips,fee_pips=fee_pips,entry=entry,target=target,risk_pips=risk/PIP,entry_time=str(t))
    prev=t-STEP
    for j in range(start,min(start+max_bars,len(f))):
        u=f.index[j]; bar=f.iloc[j]
        if u-prev!=STEP:
            return dict(result,status='censored_gap',pnl_pips=None)
        prev=u
        op,hi,lo=(bar.bid_open,bar.bid_high,bar.bid_low) if side==1 else (bar.ask_open,bar.ask_high,bar.ask_low)
        if (op-stop)*side<=0:
            exit_price=float(op); reason='stop_gap_open'
        elif (op-target)*side>=0:
            exit_price=target; reason='target_gap_open'
        elif (lo<=stop if side==1 else hi>=stop):
            exit_price=stop; reason='stop' # stop first if both levels touched
        elif (hi>=target if side==1 else lo<=target):
            exit_price=target; reason='target'
        elif j==start+max_bars-1:
            exit_price=float(bar.bid_close if side==1 else bar.ask_close); reason='time_exit'
        else:
            continue
        if reason not in ('target','target_gap_open'): exit_price-=side*slip
        pnl=(exit_price-entry)*side/PIP-fee_pips
        return dict(result,status='win' if pnl>1e-8 else 'loss' if pnl < -1e-8 else 'flat',
                    exit=exit_price,exit_time=str(u+STEP),reason=reason,pnl_pips=pnl,r_multiple=pnl/(risk/PIP))
    return dict(result,status='censored_end',pnl_pips=None)
