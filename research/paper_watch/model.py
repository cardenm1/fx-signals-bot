"""Load frozen baseline and expose diagnostics; no alternate strategy variants."""
import importlib.util,json,hashlib
from pathlib import Path
import numpy as np
import pandas as pd
FROZEN=Path(__file__).with_name('frozen')
spec=importlib.util.spec_from_file_location('paper_frozen',FROZEN/'recognize.py');ref=importlib.util.module_from_spec(spec);spec.loader.exec_module(ref)
CFG=json.loads((FROZEN/'recognition_plan.json').read_text())['configs']['reference']
BAR=pd.Timedelta(minutes=5)

def check_model():
    manifest=json.loads((FROZEN/'manifest.json').read_text())
    for name,want in manifest.items():
        if hashlib.sha256((FROZEN/name).read_bytes()).hexdigest()!=want:raise ValueError('Frozen baseline changed: '+name)
    return manifest

def progress(c,b,f,snaps):
    """Diagnostics mirror reference state; actual signals always come from ref."""
    start=b.index.searchsorted(pd.Timestamp(c['h1_confirm']));side=c['side'];state=None;counts={'sweeps':0,'no_same_bar_return':0,'BOS_rejected_D1':0};stop='END'
    if start>=len(b) or b.index[start]!=pd.Timestamp(c['h1_confirm']):return None,counts,'MISSING_FIRST_M5',None
    segment=f.segment.iloc[start];matched=None
    for i in range(start,len(b)):
        if b.index[i]+BAR>pd.Timestamp(c['h1_confirm'])+pd.Timedelta(hours=6):stop='TTL';break
        if f.segment.iloc[i]!=segment:stop='GAP';break
        atr=f.atr_before.iloc[i]
        if i==0 or not np.isfinite(atr) or pd.isna(f.hi_i.iloc[i]) or pd.isna(f.lo_i.iloc[i]):continue
        prev=b.close.iloc[i-1];close=b.close.iloc[i]
        if state is not None:
            if i>state['return_i'] and side*(prev-state['bos_level'])<=0 and side*(close-state['bos_level'])>.05*atr:
                snap=ref.snapshot_at(snaps,b.index[i]+BAR)
                if snap and snap['valid'] and snap['structure']==side:matched=str(b.index[i]+BAR);break
                counts['BOS_rejected_D1']+=1
        else:
            level=f.lo.iloc[i] if side==1 else f.hi.iloc[i]
            pierce=b.low.iloc[i]<level-.1*atr if side==1 else b.high.iloc[i]>level+.1*atr
            if pierce and side*(prev-level)>=0:
                counts['sweeps']+=1
                if side*(close-level)<0:counts['no_same_bar_return']+=1;continue
                state={'sweep_i':i,'return_i':i,'sweep_level':float(level),'bos_level':float(f.hi.iloc[i] if side==1 else f.lo.iloc[i])}
    return state,counts,stop,matched

def analyse(b,a):
    check_model();d,snaps,pivots,levels=ref.daily_model(b);h=ref.h1_model(b);f=ref.m5_features(b);contexts=ref.h1_contexts(h,snaps,CFG);models=[];diagnostics=[]
    for c in contexts:
        s=ref.m5_confirmation(c,CFG,b,a,f,snaps);state,counts,stop,matched=progress(c,b,f,snaps)
        if matched!=(s['signal_time'] if s else None):raise ValueError('Diagnostic/reference parity failure')
        diagnostics.append((c,state,counts,stop,s is not None))
        if s:
            signal={'configuration':CFG,'direction':'BUY' if c['side']==1 else 'SELL',**c,**s}
            ref.validate_match(signal,b,d,h,snaps);models.append(signal)
    return d,snaps,h,levels,models,diagnostics

def context_key(c):return c['level']['id']+'|'+c['h1_open']+'|'+str(c['side'])

def hour_diagnostic(h,i,snaps,contexts):
    """First-failed gate counts per active level-hour, matching frozen order."""
    if not h.valid.iloc[i] or not h.valid.iloc[i-1] or h.index[i]-h.index[i-1]!=pd.Timedelta(hours=1):return 'H1_INCOMPLETE_OR_GAP',{}
    atr=h.atr_before.iloc[i]
    if not np.isfinite(atr):return 'H1_ATR_WARMUP',{}
    snap=ref.snapshot_at(snaps,h.index[i])
    if not snap or not snap['valid']:return 'D1_MISSING_OR_INCOMPLETE',{}
    if not snap['structure']:return 'D1_NEUTRAL',{}
    side=snap['structure'];counts={};add=lambda key:counts.__setitem__(key,counts.get(key,0)+1)
    for lev in snap['levels']:
        if lev['type']!=('high' if side==1 else 'low'):add('LEVEL_WRONG_DIRECTION');continue
        if len(lev['reactions'])<2:add('FEWER_TWO_REACTIONS');continue
        if lev['age']>60:add('LEVEL_TOO_OLD');continue
        if any(c['level']['id']==lev['id'] and c['side']==side and pd.Timestamp(c['h1_open']).date()==h.index[i].date() and pd.Timestamp(c['h1_open'])<h.index[i] for c in contexts):add('LEVEL_DAY_ALREADY_USED');continue
        edge=lev['anchor']+side*lev['tol']
        if side*(h.close.iloc[i-1]-edge)>0:add('PREVIOUS_CLOSE_ALREADY_BEYOND');continue
        if side*(h.close.iloc[i]-edge)<=.05*atr:add('NO_H1_CLOSE_BREAK_WITH_BUFFER');continue
        add('H1_CONTEXT_CONFIRMED')
    actual=sum(c['h1_i']==i for c in contexts)
    if counts.get('H1_CONTEXT_CONFIRMED',0)!=actual:raise ValueError('H1 diagnostics/reference parity failure')
    return ('H1_CONTEXT_CONFIRMED' if actual else 'NO_NEW_H1_CONTEXT'),counts
