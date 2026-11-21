"""Author-material research revision. All non-author numeric rules are assumptions.
State: closed HTF impulse -> later block touch -> confirmed 4-point break ->
later correction and body engulfing -> signal. BUY is a mirrored assumption.
"""
from dataclasses import dataclass, asdict
from collections import defaultdict
import hashlib
import pandas as pd
from .data import bid_bars, STEP
from . import VERSION

PIP=.0001
POINT=.00001

@dataclass(frozen=True)
class Config:
    high_minutes: int=60
    low_minutes: int=5
    pivot_radius: int=2
    block_lookback: int=6
    expiry_hours: int=72
    stop_buffer_pips: float=1.

@dataclass(frozen=True)
class Pivot:
    time: pd.Timestamp
    known: pd.Timestamp
    kind: str
    price: float
    segment: int


def aggregate(f, minutes):
    if minutes==5: return bid_bars(f)
    g=bid_bars(f).resample(f'{minutes}min',origin='start_day',label='left',closed='left')
    h=g.agg({'open':'first','high':'max','low':'min','close':'last','segment':'first'})
    h=h[(g.close.count()==minutes//5)&(g.segment.nunique()==1)].copy()
    h['segment']=(h.index.to_series().diff()!=pd.Timedelta(minutes=minutes)).cumsum().to_numpy()
    return h


def pivots(b, minutes, radius=2):
    out=[]
    rows=list(b.itertuples())
    for i in range(radius,len(rows)-radius):
        window=rows[i-radius:i+radius+1]; p=rows[i]
        if window[0].segment!=window[-1].segment: continue
        high=all(p.high>x.high for x in window if x.Index!=p.Index)
        low=all(p.low<x.low for x in window if x.Index!=p.Index)
        if high==low: continue
        out.append(Pivot(p.Index,rows[i+radius].Index+pd.Timedelta(minutes=minutes),
                         'H' if high else 'L',float(p.high if high else p.low),int(p.segment)))
    return out


def append(seq,p):
    if seq and seq[-1].segment!=p.segment: seq.clear()
    if seq and seq[-1].kind==p.kind:
        sign=1 if p.kind=='H' else -1
        if (p.price-seq[-1].price)*sign<=0: return False
        seq[-1]=p
    else: seq.append(p)
    del seq[:-4]
    return True


def four_points(seq,side):
    if len(seq)!=4: return False
    a,b,c,d=seq
    return ([x.kind for x in seq]==(['L','H','L','H'] if side==1 else ['H','L','H','L'])
            and (c.price-a.price)*side>=POINT-1e-10
            and (d.price-b.price)*side>=POINT-1e-10)


def engulf(previous,current,side):
    """Opposite-color previous body fully covered; at least one strict edge."""
    po,pc=float(previous.open),float(previous.close)
    co,cc=float(current.open),float(current.close)
    if (pc-po)*side>=0 or (cc-co)*side<=0: return False
    pl,ph=sorted((po,pc));cl,ch=sorted((co,cc))
    return cl<=pl+1e-10 and ch>=ph-1e-10 and (cl<pl-1e-10 or ch>ph+1e-10)


def signals(f,config=None,with_events=False):
    cfg=config or Config()
    if (cfg.high_minutes,cfg.low_minutes) not in ((60,5),(240,15)):
        raise ValueError('Supported primary pairs: H1/M5 and H4/M15')
    h=aggregate(f,cfg.high_minutes); l=aggregate(f,cfg.low_minutes)
    hp=defaultdict(list);lp=defaultdict(list)
    for p in pivots(h,cfg.high_minutes,cfg.pivot_radius):hp[p.known].append(p)
    for p in pivots(l,cfg.low_minutes,cfg.pivot_radius):lp[p.known].append(p)
    he={t+pd.Timedelta(minutes=cfg.high_minutes):i for i,t in enumerate(h.index)}
    le={t+pd.Timedelta(minutes=cfg.low_minutes):i for i,t in enumerate(l.index)}
    ht=list(h.itertuples());lt=list(l.itertuples())
    latest={};contexts={};used=set();out=[];events=[];segment=None
    pair=f'H{cfg.high_minutes//60}/M{cfg.low_minutes}'
    def event(kind,ctx,now):
        events.append(dict(event=kind,context_id=ctx['id'],time=str(now),direction='long' if ctx['side']==1 else 'short',pair=pair))
    for r in f.itertuples():
        t=r.Index; now=t+STEP
        if r.segment!=segment:
            for ctx in contexts.values(): event('gap_reset',ctx,now)
            latest.clear();contexts.clear()
        segment=r.segment
        # Every M5 is closed at now; no same-bar create-and-touch or future extrema.
        for key,ctx in list(contexts.items()):
            side=ctx['side']
            invalid=r.bid_low<ctx['invalid'] if side==1 else r.bid_high>ctx['invalid']
            if invalid or now>ctx['expiry']:
                event('invalidated' if invalid else 'expired',ctx,now);contexts.pop(key);continue
            if ctx['touch'] is None:
                touched=t>=ctx['known'] and r.bid_high>=ctx['zone_low'] and r.bid_low<=ctx['zone_high']
                if touched:
                    ctx['touch']=t;ctx['touch_known']=now
                    ctx['target5']=ctx['extreme']+side*POINT
                    ctx['target5_frozen']=now
                    event('block_touch',ctx,now)
                else:
                    val=float(r.bid_high if side==1 else r.bid_low)
                    if (val-ctx['extreme'])*side>0:ctx['extreme']=val
        for p in hp.get(now,[]): latest[p.kind]=p
        if now in he:
            i=he[now];bar=ht[i]
            for side in (-1,1):
                level=latest.get('L' if side==-1 else 'H')
                origin=latest.get('H' if side==-1 else 'L')
                if level is None or origin is None or not level.time<origin.time<bar.Index:continue
                if (bar.close-level.price)*side<POINT-1e-10:continue
                identity=f'{VERSION}|{pair}|{side}|{level.time}|{origin.time}'
                key=hashlib.sha256(identity.encode()).hexdigest()[:24]
                if key in used:continue
                used.add(key)
                oi=h.index.get_loc(origin.time)
                window=ht[max(0,oi-cfg.block_lookback+1):oi+1]
                candidates=[x for x in window if x.segment==bar.segment and (x.close-x.open)*side<0]
                if not candidates:continue
                block=candidates[-1]
                # Breakout must leave the block before a return can be observed.
                if not (bar.close<block.low if side==-1 else bar.close>block.high):continue
                extreme=float(max(x.high for x in ht[oi:i+1]) if side==1 else min(x.low for x in ht[oi:i+1]))
                ctx=dict(id=key,side=side,known=now,expiry=now+pd.Timedelta(hours=cfg.expiry_hours),
                         zone_low=float(block.low),zone_high=float(block.high),block_time=block.Index,
                         origin=origin,level=level,break_bar=bar.Index,
                         invalid=float(block.high+cfg.stop_buffer_pips*PIP if side==-1 else block.low-cfg.stop_buffer_pips*PIP),
                         extreme=extreme,touch=None,seq=[],pattern=None)
                contexts[key]=ctx;event('impulse_confirmed',ctx,now)
        for p in lp.get(now,[]):
            for ctx in contexts.values():
                if ctx['touch'] is None or ctx['pattern'] is not None or p.time<ctx['touch_known']:continue
                if not append(ctx['seq'],p) or not four_points(ctx['seq'],ctx['side']):continue
                a,b,c,d=ctx['seq']
                if not ctx['zone_low']<=a.price<=ctx['zone_high']:continue
                # A low/high wick alone is insufficient: require a closed break of p2,
                # after p3 becomes available, by the time p4 is confirmed.
                part=l[(l.index>=c.known)&(l.index+pd.Timedelta(minutes=cfg.low_minutes)<=d.known)]
                if not ((part.close-b.price)*ctx['side']>=POINT-1e-10).any():continue
                ctx['pattern']=(a,b,c,d);ctx['pattern_known']=now
                event('four_point_break',ctx,now)
        if now not in le:continue
        j=le[now]
        if j<1:continue
        bar=lt[j];previous=lt[j-1]
        if bar.Index-previous.Index!=pd.Timedelta(minutes=cfg.low_minutes):continue
        for key,ctx in list(contexts.items()):
            if ctx['pattern'] is None:continue
            a,b,c,d=ctx['pattern'];side=ctx['side']
            # At least a full later correction candle then a later engulfing candle.
            if previous.Index<ctx['pattern_known'] or not engulf(previous,bar,side):continue
            # Do not chase an entry whose near extremum has already been passed.
            if (bar.close-d.price)*side>=0:continue
            correction=l[(l.index>=d.time)&(l.index<=bar.Index)]
            stop=float(min(c.price,correction.low.min())-cfg.stop_buffer_pips*PIP if side==1 else max(c.price,correction.high.max())+cfg.stop_buffer_pips*PIP)
            s=dict(signal_id=key,strategy=VERSION,pair=pair,direction='long' if side==1 else 'short',
                   variant='impulse_block_four_points_engulf',signal_time=str(now),signal_bar_open=str(bar.Index),
                   stop=stop,point2=b.price,zone_low=ctx['zone_low'],zone_high=ctx['zone_high'],
                   context_known=str(ctx['known']),block_time=str(ctx['block_time']),break_bar=str(ctx['break_bar']),
                   touch_time=str(ctx['touch']),touch_known=str(ctx['touch_known']),pattern_known=str(ctx['pattern_known']),
                   engulf_previous=str(previous.Index),target5=ctx['target5'],target5_frozen=str(ctx['target5_frozen']),
                   structure_level=ctx['level'].price,origin_time=str(ctx['origin'].time))
            for n,p in enumerate((a,b,c,d),1):
                s.update({f'p{n}_time':str(p.time),f'p{n}_known':str(p.known),f'p{n}_price':p.price})
            out.append(s);event('engulf_entry_signal',ctx,now);contexts.pop(key)
    return (out,events) if with_events else out
