"""D1 -> H1 -> M5 recognition only. No execution, PnL or optimization."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[2]
PIP=.0001
BAR=pd.Timedelta(minutes=5)


def load():
    frames=[]
    for side in ['BID','ASK']:
        d=pd.read_csv(ROOT/f'research/data/GBPUSD_{side}_M5_CLEAN.csv',index_col=0)
        d.index=pd.to_datetime(d.index,utc=True);d.columns=d.columns.str.lower()
        d=d[['open','high','low','close']].apply(pd.to_numeric)
        assert d.index.is_monotonic_increasing and not d.index.has_duplicates
        assert np.isfinite(d.to_numpy()).all()
        frames.append(d)
    b,a=frames;assert b.index.equals(a.index)
    assert (a[['open','close']]>=b[['open','close']]).all().all()
    return b,a


def true_range(d):
    prev=d.close.shift(1)
    return pd.concat([d.high-d.low,(d.high-prev).abs(),(d.low-prev).abs()],axis=1).max(axis=1)


def pivot_masks(d,valid=None,segment=None):
    if valid is None:valid=pd.Series(True,index=d.index)
    if segment is None:segment=pd.Series(0,index=d.index)
    hi=valid.copy();lo=valid.copy()
    for k in [-2,-1,1,2]:
        hi &= valid.groupby(segment).shift(k,fill_value=False)&(d.high>d.high.groupby(segment).shift(k))
        lo &= valid.groupby(segment).shift(k,fill_value=False)&(d.low<d.low.groupby(segment).shift(k))
    return hi,lo


def closed_touch_rejection(kind,anchor,tol,low,high,close):
    touch=low<=anchor+tol and high>=anchor-tol
    reject=close<anchor-tol if kind=='high' else close>anchor+tol
    return touch and reject


def daily_model(b):
    grid=pd.date_range(b.index[0].floor('D'),b.index[-1].floor('D')+pd.Timedelta(days=1),freq='5min',inclusive='left')
    ny=grid.tz_convert('America/New_York');wd=ny.dayofweek;hour=ny.hour
    active=(wd<4)|((wd==4)&(hour<17))|((wd==6)&(hour>=17))
    expected=pd.Series(active,index=grid).resample('1D').sum()
    d=b.resample('1D').agg({'open':'first','high':'max','low':'min','close':'last'})
    d['actual']=b.close.resample('1D').count();d['expected']=expected
    d=d[d.expected>0].copy();d['valid']=d.actual.eq(d.expected)
    # Exact active-grid equality, not merely matching counts.
    active_grid=pd.Series(grid[active],index=grid[active]).groupby(grid[active].floor('D'))
    for day,values in active_grid:
        observed=b.index[b.index.floor('D')==day]
        if day in d.index:d.loc[day,'valid']=observed.equals(pd.DatetimeIndex(values.to_numpy()))
    good=d[d.valid]
    d['atr']=true_range(good).rolling(14,min_periods=14).mean().reindex(d.index)
    d['ema220']=good.close.ewm(span=220,adjust=False,min_periods=220).mean().reindex(d.index)
    d['ema_count']=np.cumsum(d.valid.to_numpy())
    ph,pl=pivot_masks(d,d.valid)
    snapshots=[];pivots=[];levels=[];counter=0
    for i in range(len(d)):
        known=d.index[i]+pd.Timedelta(days=1)
        for lev in levels:
            if lev['active'] and (i-lev['seed_i']>60 or (d.valid.iloc[i] and
                ((lev['type']=='high' and d.close.iloc[i]>lev['anchor']+lev['tol']) or
                 (lev['type']=='low' and d.close.iloc[i]<lev['anchor']-lev['tol'])))):
                lev['active']=False;lev['invalidated_at']=str(known)
        if i>=4 and d.valid.iloc[i-4:i+1].all():
            p=i-2
            for kind,mask,col in [('high',ph,'high'),('low',pl,'low')]:
                if not mask.iloc[p]:continue
                price=float(d[col].iloc[p]);reaction={'type':kind,'pivot_i':p,
                    'pivot_open':str(d.index[p]),'price':price,'known_at':str(known),'confirmation_i':i}
                pivots.append(reaction)
                if pd.isna(d.atr.iloc[i]):continue
                candidates=[lev for lev in levels if lev['active'] and lev['type']==kind and
                            abs(price-lev['anchor'])<=lev['tol']]
                if not candidates:
                    counter+=1
                    reaction['reaction_kind']='seed_swing'
                    levels.append({'id':f'L{counter:03d}','type':kind,'anchor':price,
                                   'tol':float(.2*d.atr.iloc[i]),'seed_i':p,'created_at':str(known),
                                   'reactions':[copy.deepcopy(reaction)],'active':True})
        if d.valid.iloc[i]:
            for lev in levels:
                if not lev['active'] or pd.Timestamp(lev['created_at'])>=known:continue
                if i-lev['reactions'][-1]['pivot_i']<3:continue
                low,high=lev['anchor']-lev['tol'],lev['anchor']+lev['tol']
                if closed_touch_rejection(lev['type'],lev['anchor'],lev['tol'],
                                          d.low.iloc[i],d.high.iloc[i],d.close.iloc[i]):
                    lev['reactions'].append({'type':lev['type'],'pivot_i':i,
                        'pivot_open':str(d.index[i]),
                        'price':float(d.high.iloc[i] if lev['type']=='high' else d.low.iloc[i]),
                        'known_at':str(known),'confirmation_i':i,'reaction_kind':'touch_rejection'})
        highs=[p for p in pivots if p['type']=='high'];lows=[p for p in pivots if p['type']=='low']
        structure=0
        if d.valid.iloc[i] and len(highs)>=2 and len(lows)>=2:
            hh=highs[-1]['price']-highs[-2]['price'];ll=lows[-1]['price']-lows[-2]['price']
            structure=1 if hh>0 and ll>0 else (-1 if hh<0 and ll<0 else 0)
        ema=0
        if d.valid.iloc[i] and pd.notna(d.ema220.iloc[i]):
            ema=1 if d.close.iloc[i]>d.ema220.iloc[i] else (-1 if d.close.iloc[i]<d.ema220.iloc[i] else 0)
        live=[]
        for lev in levels:
            if lev['active']:
                record=copy.deepcopy(lev);record['age']=i-lev['seed_i'];live.append(record)
        snapshots.append({'i':i,'known_at':known,'valid':bool(d.valid.iloc[i]),'structure':structure,
                          'ema220':ema,'intersection':structure if structure==ema else 0,
                          'levels':live,'trend_pivots':copy.deepcopy(highs[-2:]+lows[-2:])})
    return d,snapshots,pivots,levels


def h1_model(b):
    h=b.resample('1h').agg({'open':'first','high':'max','low':'min','close':'last'})
    h['count']=b.close.resample('1h').count();h=h[h['count']>0].copy();h['valid']=h['count'].eq(12)
    good=h[h.valid]
    h['atr_before']=true_range(good).rolling(14,min_periods=14).mean().shift(1).reindex(h.index)
    return h


def m5_features(b):
    seg=b.index.to_series().diff().ne(BAR).cumsum();ph,pl=pivot_masks(b,segment=seg)
    f=pd.DataFrame({'segment':seg},index=b.index)
    pieces=[]
    for _,g in b.groupby(seg):pieces.append(true_range(g).rolling(14,min_periods=14).mean().shift(1))
    f['atr_before']=pd.concat(pieces).reindex(b.index)
    for kind,mask,col in [('hi',ph,'high'),('lo',pl,'low')]:
        positions=pd.Series(np.where(mask,np.arange(len(b)),np.nan),index=b.index)
        # Confirm p after p+2 CLOSE; use only pivots known at candidate bar OPEN.
        pos=positions.groupby(seg).shift(3).groupby(seg).ffill()
        f[kind+'_i']=pos
        f[kind]=[float(b[col].iloc[int(p)]) if pd.notna(p) else np.nan for p in pos]
    return f


def snapshot_at(snapshots,t):
    known=pd.DatetimeIndex([s['known_at'] for s in snapshots]);j=known.searchsorted(t,side='right')-1
    return snapshots[j] if j>=0 else None


def h1_contexts(h,snapshots,cfg):
    contexts=[];used=set()
    for i in range(1,len(h)):
        if not h.valid.iloc[i] or not h.valid.iloc[i-1] or h.index[i]-h.index[i-1]!=pd.Timedelta(hours=1):continue
        atr=h.atr_before.iloc[i]
        if not np.isfinite(atr):continue
        snap=snapshot_at(snapshots,h.index[i])
        if not snap or not snap['valid']:continue
        side=snap[cfg['trend']]
        if not side:continue
        kind='high' if side==1 else 'low'
        for lev in snap['levels']:
            if lev['type']!=kind or len(lev['reactions'])<cfg['reactions'] or lev['age']>cfg['age']:continue
            key=(lev['id'],str(h.index[i].date()),side)
            if key in used:continue
            edge=lev['anchor']+side*lev['tol'];buffer=.05*atr
            previous=h.close.iloc[i-1]
            if side*(previous-edge)>0:continue
            beyond=side*(h.close.iloc[i]-edge)>buffer
            wick=(h.high.iloc[i]>edge+buffer) if side==1 else (h.low.iloc[i]<edge-buffer)
            if cfg['h1']=='wick' and not wick:continue
            if cfg['h1']!='wick' and not beyond:continue
            final_i=i;retest_i=None
            if cfg['h1']=='retest':
                found=False
                for r in range(i+1,min(i+7,len(h))):
                    if not h.valid.iloc[r] or h.index[r]-h.index[r-1]!=pd.Timedelta(hours=1):break
                    touch=h.low.iloc[r]<=lev['anchor']+lev['tol'] and h.high.iloc[r]>=lev['anchor']-lev['tol']
                    if touch and side*(h.close.iloc[r]-edge)>0:
                        final_i=r;retest_i=r;found=True;break
                if not found:continue
            confirm=h.index[final_i]+pd.Timedelta(hours=1)
            contexts.append({'side':side,'h1_i':i,'h1_retest_i':retest_i,'h1_confirm':str(confirm),
                             'h1_open':str(h.index[i]),'h1_break_known':str(h.index[i]+pd.Timedelta(hours=1)),
                             'd1_snapshot_i':snap['i'],'d1_known':str(snap['known_at']),
                             'level':copy.deepcopy(lev),'h1_mode':cfg['h1']})
            used.add(key)
    return sorted(contexts,key=lambda x:(x['h1_confirm'],x['level']['id']))


def m5_confirmation(context,cfg,b,a,f,snapshots):
    confirm=pd.Timestamp(context['h1_confirm']);start=b.index.searchsorted(confirm)
    if start>=len(b) or b.index[start]!=confirm:return None
    segment=f.segment.iloc[start];side=context['side'];state=None
    for i in range(start,len(b)):
        if b.index[i]+BAR>confirm+pd.Timedelta(hours=6) or f.segment.iloc[i]!=segment:break
        atr=f.atr_before.iloc[i]
        if i==0 or not np.isfinite(atr) or pd.isna(f.hi_i.iloc[i]) or pd.isna(f.lo_i.iloc[i]):continue
        close=b.close.iloc[i];prev=b.close.iloc[i-1];signal=None
        if cfg['m5']=='simple':
            level=f.hi.iloc[i] if side==1 else f.lo.iloc[i]
            if side*(prev-level)<=0 and side*(close-level)>.05*atr:
                p=int(f.hi_i.iloc[i] if side==1 else f.lo_i.iloc[i])
                signal={'bos_i':i,'bos_level':float(level),'bos_pivot_i':p,'bos_pivot_known':str(b.index[p]+3*BAR),
                        'sweep_i':None,'return_i':None,'sweep_level':None,'sweep_pivot_i':None}
        else:
            if state is not None:
                if state['phase']=='return':
                    if i-state['sweep_i']>cfg['return_max']:state=None
                    elif side*(close-state['sweep_level'])>=0:
                        state['return_i']=i;state['phase']='bos'
                    continue
                if i>state['return_i'] and side*(prev-state['bos_level'])<=0 and side*(close-state['bos_level'])>.05*atr:
                    signal={k:v for k,v in state.items() if k!='phase'};signal['bos_i']=i
            if state is None and signal is None:
                sweep_level=f.lo.iloc[i] if side==1 else f.hi.iloc[i]
                pierce=b.low.iloc[i]<sweep_level-.1*atr if side==1 else b.high.iloc[i]>sweep_level+.1*atr
                if pierce and side*(prev-sweep_level)>=0:
                    same=side*(close-sweep_level)>=0
                    if (cfg['return_max']==0 and not same) or (cfg['return_max']>0 and same):continue
                    sp=int(f.lo_i.iloc[i] if side==1 else f.hi_i.iloc[i])
                    bp=int(f.hi_i.iloc[i] if side==1 else f.lo_i.iloc[i])
                    state={'phase':'bos' if same else 'return','sweep_i':i,
                           'return_i':i if same else None,'sweep_level':float(sweep_level),
                           'sweep_pivot_i':sp,'sweep_pivot_known':str(b.index[sp]+3*BAR),
                           'bos_level':float(f.hi.iloc[i] if side==1 else f.lo.iloc[i]),
                           'bos_pivot_i':bp,'bos_pivot_known':str(b.index[bp]+3*BAR)}
        if signal is None:continue
        time=b.index[i]+BAR;snap=snapshot_at(snapshots,time)
        if not snap or not snap['valid'] or snap[cfg['trend']]!=side:continue
        price=float(a.close.iloc[i] if side==1 else b.close.iloc[i])
        signal.update({'signal_time':str(time),'candidate_quote':price,'quote_side':'ASK' if side==1 else 'BID',
                       'spread_pips':float((a.close.iloc[i]-b.close.iloc[i])/PIP),
                       'd1_signal_snapshot_i':snap['i'],'d1_signal_known':str(snap['known_at'])})
        return signal
    return None


def recognize(b,a,configs):
    d,snapshots,pivots,levels=daily_model(b);h=h1_model(b);f=m5_features(b)
    matches=[];counts=[]
    for name,cfg in configs.items():
        contexts=h1_contexts(h,snapshots,cfg);found=[]
        for context in contexts:
            signal=m5_confirmation(context,cfg,b,a,f,snapshots)
            if signal:
                match={'config':name,'configuration':cfg,'direction':'BUY' if context['side']==1 else 'SELL',
                       **context,**signal}
                found.append(match);matches.append(match)
        counts.append({'config':name,**cfg,'h1_contexts':len(contexts),'BUY':sum(s['side']==1 for s in found),
                       'SELL':sum(s['side']==-1 for s in found),'total':len(found)})
        print(name,'contexts',len(contexts),'BUY',counts[-1]['BUY'],'SELL',counts[-1]['SELL'],flush=True)
    return matches,pd.DataFrame(counts),d,h,f,snapshots,pivots,levels


def select_examples(matches):
    order=['reference','h1_retest','h1_wick','return_next1','return_next2','return_next3',
           'three_reactions','young_level20','trend_ema220','trend_intersection','m5_simple']
    selected=[];used=set()
    for direction in ['BUY','SELL']:
        count=0;used_day_level=set()
        for cfg in order:
            for s in sorted((m for m in matches if m['direction']==direction and m['config']==cfg),key=lambda m:m['signal_time']):
                key=(s['signal_time'],direction,s['level']['id'])
                day_level=(str(pd.Timestamp(s['signal_time']).date()),s['level']['id'])
                if key in used or day_level in used_day_level:continue
                selected.append(s);used.add(key);used_day_level.add(day_level);count+=1
                if count==5:break
            if count==5:break
    # Extra examples illustrate alternative recognition rules, not profitable outcomes.
    for cfg in ['h1_wick','h1_retest','return_next3','m5_simple','trend_ema220','three_reactions']:
        if any(s['config']==cfg for s in selected):continue
        pool=sorted((m for m in matches if m['config']==cfg),key=lambda m:m['signal_time'])
        for s in pool:
            key=(s['signal_time'],s['direction'],s['level']['id'])
            if key not in used:selected.append(s);used.add(key);break
    for i,s in enumerate(selected,1):s['example_id']=f'E{i:02d}'
    return selected


def validate_match(s,b,d,h,snapshots):
    t=pd.Timestamp(s['signal_time']);start=pd.Timestamp(s['h1_open']);conf=pd.Timestamp(s['h1_confirm'])
    assert pd.Timestamp(s['d1_known'])<=start
    assert pd.Timestamp(s['level']['created_at'])<=start
    for reaction in s['level']['reactions']:assert pd.Timestamp(reaction['known_at'])<=start
    assert len(s['level']['reactions'])>=s['configuration']['reactions']
    assert conf<=b.index[s['bos_i']]
    assert pd.Timestamp(s['bos_pivot_known'])<=b.index[s['sweep_i'] if s['sweep_i'] is not None else s['bos_i']]
    if s['sweep_i'] is not None:
        assert conf<=b.index[s['sweep_i']]
        assert pd.Timestamp(s['sweep_pivot_known'])<=b.index[s['sweep_i']]
        assert s['bos_i']>s['return_i']>=s['sweep_i']
        n=s['return_i']-s['sweep_i']
        assert n==0 if s['configuration']['return_max']==0 else 1<=n<=s['configuration']['return_max']
    assert b.index[s['bos_i']]+BAR==t
    assert pd.Timestamp(s['d1_signal_known'])<=t
    assert snapshots[s['d1_signal_snapshot_i']][s['configuration']['trend']]==s['side']
    return True


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',required=True)
    args=p.parse_args();out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    plan=json.loads(Path(__file__).with_name('recognition_plan.json').read_text())
    for side in ['BID','ASK']:
        assert hashlib.sha256((ROOT/f'research/data/GBPUSD_{side}_M5_CLEAN.csv').read_bytes()).hexdigest()==plan['inputs'][side]
    assert plan['phase3_authorized'] is False
    b,a=load();matches,counts,d,h,f,snapshots,pivots,levels=recognize(b,a,plan['configs'])
    for s in matches:validate_match(s,b,d,h,snapshots)
    examples=select_examples(matches)
    for s in examples:validate_match(s,b,d,h,snapshots)
    counts.to_csv(out/'recognition_counts.csv',index=False);d.to_csv(out/'D1_audit.csv')
    def json_safe(value):
        if isinstance(value,pd.Timestamp):return str(value)
        if isinstance(value,(np.integer,np.bool_)):return value.item()
        if isinstance(value,np.floating):return float(value)
        raise TypeError(type(value).__name__)
    for filename,value in [('all_models.json',matches),('examples.json',examples),('daily_pivots.json',pivots)]:
        (out/filename).write_text(json.dumps(value,ensure_ascii=False,indent=2,default=json_safe))
    compact=[]
    for s in matches:
        compact.append({'config':s['config'],'direction':s['direction'],'signal_time':s['signal_time'],
                        'quote':s['candidate_quote'],'quote_side':s['quote_side'],'spread_pips':s['spread_pips'],
                        'level_id':s['level']['id'],'level_price':s['level']['anchor'],
                        'level_reactions_known':len(s['level']['reactions']),'level_age_d1':s['level']['age'],
                        'h1_confirm':s['h1_confirm'],'sweep_i':s['sweep_i'],'return_i':s['return_i'],
                        'bos_i':s['bos_i']})
    pd.DataFrame(compact).to_csv(out/'models.csv',index=False)
    metadata={'stage':'STOP_AFTER_VISUAL_REVIEW','phase3_authorized':False,'models':len(matches),
              'examples':len(examples),'examples_BUY':sum(s['side']==1 for s in examples),
              'examples_SELL':sum(s['side']==-1 for s in examples),'valid_D1':int(d.valid.sum()),
              'short_valid_D1':int((d.valid&(d.actual<288)).sum()),'invalid_D1':int((~d.valid).sum()),
              'ema220_available_D1':int(d.ema220.notna().sum()),
              'first_ema220_known':str(d[d.ema220.notna()].index[0]+pd.Timedelta(days=1)) if d.ema220.notna().any() else None,
              'd1_boundary':'UTC00','timestamp_semantics':'OPEN_ASSUMED_UNVERIFIED',
              'all_model_causality_checks':'PASSED','no_performance_calculated':True}
    (out/'metadata.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2))
    (out/'recognition_plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2))
    print(json.dumps(metadata,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
