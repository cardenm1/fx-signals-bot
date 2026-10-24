"""Event-sourced paper engine. No order routing, broker account, or Telegram."""
import hashlib,json,math,os
from pathlib import Path
import numpy as np
import pandas as pd
from .ledger import Ledger,IntegrityError,event_id,canonical
from . import model
BAR=pd.Timedelta(minutes=5);PIP=.0001
MIN_START=pd.Timestamp('2026-10-24T00:00:00Z')

def utc(value):
    t=pd.Timestamp(value)
    if t.tzinfo is None:raise ValueError('Naive timestamp forbidden')
    return t.tz_convert('UTC')
def ts(value):return utc(value).isoformat()
def clean_payload(value):return json.loads(json.dumps(value,default=lambda x:x.item() if isinstance(x,np.generic) else str(x),allow_nan=False))

def validate_pair(b,a):
    if not b.index.equals(a.index):raise ValueError('BID/ASK indices differ: no silent intersection')
    for name,d in [('BID',b),('ASK',a)]:
        if not isinstance(d.index,pd.DatetimeIndex) or d.index.tz is None:raise ValueError(name+' requires explicit timezone')
        if not d.index.is_monotonic_increasing or d.index.has_duplicates:raise ValueError(name+' duplicate/unsorted bars')
        if not d.index.equals(d.index.floor('5min')):raise ValueError(name+' off M5 grid')
        x=d[['open','high','low','close']]
        if not np.isfinite(x.to_numpy()).all() or (x.to_numpy()<=0).any():raise ValueError(name+' invalid prices')
        if ((x.high<x.max(axis=1))|(x.low>x.min(axis=1))).any():raise ValueError(name+' invalid OHLC')
    if (a[['open','high','low','close']]<b[['open','high','low','close']]).any().any():raise ValueError('ASK below BID')

def read_pair(bid,ask):
    frames=[]
    for path in [bid,ask]:
        raw=pd.read_csv(path,index_col=0)
        # pd.to_datetime(utc=True) alone would silently localize naive labels.
        if not all(pd.Timestamp(x).tzinfo is not None for x in raw.index):raise ValueError('Naive CSV index')
        raw.index=pd.to_datetime(raw.index,utc=True);frames.append(raw[['open','high','low','close']].astype(float))
    validate_pair(*frames);return frames

def runtime_manifest():
    folder=Path(__file__).parent
    return {name:hashlib.sha256((folder/name).read_bytes()).hexdigest() for name in ['__init__.py','engine.py','ledger.py','model.py','feed.py','cli.py','market.py']}

def initialize(root,b,a,now,slippage_pips=0.0):
    now=utc(now);validate_pair(b,a);model.check_model()
    if b.empty:raise ValueError('Warmup history required')
    if b.index[-1]+BAR>now:raise ValueError('Warmup contains an unfinished candle')
    grid=pd.date_range(now.floor('D')-pd.Timedelta(days=7),now.floor('5min'),freq='5min',inclusive='left')
    ny=grid.tz_convert('America/New_York');week=ny.dayofweek
    active=(week<4)|((week==4)&(ny.hour<17))|((week==6)&(ny.hour>=17))
    expected=grid[active]
    if len(expected) and expected[-1]-b.index[-1]>pd.Timedelta(hours=1):raise ValueError('Warmup stale: run approved bootstrap first (maximum one active hour behind)')
    if not math.isfinite(slippage_pips) or slippage_pips<0:raise ValueError('Invalid slippage')
    root=Path(root)
    if root.exists() and any(root.iterdir()):raise ValueError('Session directory must be empty; never overwrite a run')
    root.mkdir(parents=True,exist_ok=True)
    hashes={}
    for side,d in [('BID',b),('ASK',a)]:
        p=root/f'warmup_{side}.csv'
        with p.open('x') as f:d.to_csv(f,index_label='timestamp');f.flush();os.fsync(f.fileno())
        hashes[side]=hashlib.sha256(p.read_bytes()).hexdigest()
    start=max(now.ceil('5min'),MIN_START)
    config={'version':1,'library_versions':{'pandas':pd.__version__,'numpy':np.__version__},'evaluation_start_utc':ts(start),'initialized_utc':ts(now),'warmup_hashes':hashes,
        'frozen_model_hashes':model.check_model(),'runtime_hashes':runtime_manifest(),'delay_seconds':300,'max_quote_age_seconds':30,
        'max_entry_lateness_seconds':60,'bar_finality_seconds':2,'slippage_pips':float(slippage_pips),
        'mode':'PAPER_ONLY','one_position':True,'tp_R':2,'commission_pips':0,'publication':False}
    # Journal config is authoritative, no writable config that can mutate a run.
    with Ledger(root) as journal:journal.append('INIT','session',ts(now),ts(now),config)
    return config

class Engine:
    def __init__(self,journal,model_fn=None):
        self.journal=journal;self.model_fn=model_fn or model.analyse
        initial=[r for r in journal.events if r['kind']=='INIT']
        if len(initial)!=1:raise ValueError('Initialize a new session first')
        self.config=json.loads(initial[0]['payload_json']);self.start=utc(self.config['evaluation_start_utc'])
        if self.config['frozen_model_hashes']!=model.check_model():raise ValueError('Model manifest differs from session')
        if self.config['runtime_hashes']!=runtime_manifest():raise ValueError('Observer runtime changed: new session required')
        if self.config['library_versions']!={'pandas':pd.__version__,'numpy':np.__version__}:raise ValueError('pandas/numpy versions changed since initialization')
        for side in ['BID','ASK']:
            p=journal.root/f'warmup_{side}.csv'
            if hashlib.sha256(p.read_bytes()).hexdigest()!=self.config['warmup_hashes'][side]:raise IntegrityError('Warmup changed: '+side)
        self.b,self.a=read_pair(journal.root/'warmup_BID.csv',journal.root/'warmup_ASK.csv')
        self.last_processed=self.b.index[-1]
        self.pending={};self.position=None;self.halted=False;self.last_tick=None;self.last_quote=None;self.gap=False
        extras=[]
        for row in journal.events:
            p=json.loads(row['payload_json']);k=row['kind']
            if k=='SOURCE_BATCH':
                raw_path=(journal.root/p['path']).resolve()
                if not raw_path.is_relative_to(journal.root.resolve()) or not raw_path.exists() or hashlib.sha256(raw_path.read_bytes()).hexdigest()!=p['sha256']:raise IntegrityError('Source archive missing/changed')
            if k=='BAR':extras.append(p)
            elif k=='SIGNAL':self.pending[p['signal_id']]=p
            elif k=='REJECTION':self.pending.pop(p.get('signal_id'),None)
            elif k=='ENTRY':self.position=p;self.pending.pop(p['signal_id'],None);self.gap=False
            elif k=='EXIT':self.position=None;self.gap=False
            elif k=='TICK_CURSOR':self.last_tick=utc(p['time']);self.last_quote=p
            elif k=='TRADE_GAP':self.gap=True
            elif k=='HALT':self.halted=True
            elif k=='ANALYSIS_CHECKPOINT':self.last_processed=utc(p['bar_open_utc'])
        if extras:
            ix=pd.DatetimeIndex([utc(p['time']) for p in extras])
            self.b=pd.concat([self.b,pd.DataFrame([p['bid'] for p in extras],index=ix)]).sort_index()
            self.a=pd.concat([self.a,pd.DataFrame([p['ask'] for p in extras],index=ix)]).sort_index()
            validate_pair(self.b,self.a)
    def emit(self,kind,key,event_time,now,payload):
        return self.journal.append(kind,str(key),ts(event_time),ts(now),clean_payload(payload))
    def error(self,reason,now,halt=False):
        self.emit('ERROR',ts(now)+'|'+reason,now,now,{'reason':reason})
        if self.position:
            self.emit('TRADE_GAP','error|'+ts(now),now,now,{'signal_id':self.position['signal_id'],'reason':'UNOBSERVED_AFTER_DATA_ERROR','uncertain':True});self.gap=True
        if halt:self.emit('HALT',ts(now),now,now,{'reason':reason});self.halted=True
    def reject(self,s,reason,now):
        self.emit('REJECTION',s['signal_id']+'|execution',now,now,{'signal_id':s['signal_id'],'direction':s['direction'],'reason':reason});self.pending.pop(s['signal_id'],None)
    def ticks(self,ticks,now):
        if ticks is None or ticks.empty:return
        if ticks.index.tz is None or not ticks.index.is_monotonic_increasing:raise ValueError('Ticks need monotonic timezone-aware UTC')
        if (ticks.index>now).any():raise ValueError('Future tick')
        if not np.isfinite(ticks[['bid','ask']].to_numpy()).all() or (ticks[['bid','ask']]<=0).any().any() or (ticks.ask<ticks.bid).any():raise ValueError('Invalid tick quotes')
        fresh=ticks if self.last_tick is None else ticks[ticks.index>self.last_tick]
        for t,q in fresh.iterrows():
            # Existing positions may be reconstructed from late-delivered source
            # ticks, but new entries always use the quote observed at this poll.
            if self.position and t>=utc(self.position['entry_observed_utc']):
                if self.last_tick is not None and t-self.last_tick>BAR:
                    self.emit('TRADE_GAP',self.position['signal_id']+'|'+ts(t),t,now,{'signal_id':self.position['signal_id'],'reason':'QUOTE_GAP_GT_M5','seconds':(t-self.last_tick).total_seconds(),'uncertain':True});self.gap=True
                p=self.position;side=p['side'];price=float(q.bid if side==1 else q.ask);stop=side*(price-p['SL'])<=0;target=side*(price-p['TP'])>=0
                if stop or target:
                    fill=price-side*self.config['slippage_pips']*PIP if stop else p['TP']
                    result=side*(fill-p['entry'])/p['risk_price']
                    self.emit('EXIT',p['signal_id'],t,now,{'signal_id':p['signal_id'],'direction':p['direction'],'D1_level':p['D1_level'],'entry':p['entry'],'SL':p['SL'],'TP':p['TP'],'exit':fill,'R':result,'reason':'SL_QUOTE' if stop else 'TP_LIMIT','gap_or_data_uncertainty':self.gap,'source_tick_utc':ts(t),'late_observation_seconds':(now-t).total_seconds(),'assumed_slippage_pips':self.config['slippage_pips'] if stop else 0})
                    self.position=None;self.gap=False
            self.last_tick=t
        last=ticks.iloc[-1];qt=utc(ticks.index[-1])
        if self.last_quote is None or qt>utc(self.last_quote['time']):
            self.last_quote={'time':ts(qt),'bid':float(last.bid),'ask':float(last.ask)}
            self.emit('TICK_CURSOR',ts(qt),qt,now,self.last_quote)
        # Entries are never filled at an old bar open reconstructed after restart.
        for key,s in list(self.pending.items()):
            due=utc(s['entry_due_utc'])
            if now<due:continue
            if self.position:self.reject(s,'ONE_POSITION_ONLY',now);continue
            if now-due>pd.Timedelta(seconds=self.config['max_entry_lateness_seconds']):self.reject(s,'MISSED_ENTRY_DEADLINE',now);continue
            if qt<due or now-qt>pd.Timedelta(seconds=self.config['max_quote_age_seconds']):continue
            side=s['side'];quote=float(last.ask if side==1 else last.bid);entry=quote+side*self.config['slippage_pips']*PIP;risk=side*(entry-s['SL'])
            if risk<=0:self.reject(s,'ENTRY_BEYOND_STRUCTURAL_SL',now);continue
            p={**s,'entry':entry,'TP':entry+side*2*risk,'risk_price':risk,'spread_pips':float((last.ask-last.bid)/PIP),'entry_observed_utc':ts(now),'source_tick_utc':ts(qt),'quote_age_seconds':(now-qt).total_seconds(),'entry_lateness_seconds':(now-due).total_seconds(),'assumed_slippage_pips':self.config['slippage_pips']}
            self.emit('ENTRY',key,now,now,p);self.position=p;self.pending.pop(key,None);self.gap=False
    def ingest(self,b,a,ticks,now):
        if getattr(self,'_needs_restore',False):
            self.journal.events=self.journal.read()
            self.journal.ids={r['event_id']:r for r in self.journal.events}
            self.journal.sync_views()
            restored=Engine(self.journal,self.model_fn);self.__dict__.update(restored.__dict__)
            self._needs_restore=False
        try:return self._ingest(b,a,ticks,now)
        except BaseException:
            self._needs_restore=True
            raise
    def _ingest(self,b,a,ticks,now):
        # Rebuild from authoritative commits, including writes interrupted before
        # their in-memory mutation or CSV projection completed.
        now=utc(now)
        for signal in list(self.pending.values()):
            if utc(signal['signal_received_utc'])>=utc(signal['entry_due_utc']):
                self.reject(signal,'SIGNAL_RECEIVED_TOO_LATE_NO_RETRO_ENTRY',now)
        if self.halted:raise ValueError('Session HALTED; create no new entries')
        validate_pair(b,a)
        if len(b) and (b.index+BAR>now-pd.Timedelta(seconds=self.config['bar_finality_seconds'])).any():raise ValueError('Unclosed or not-final M5 supplied')
        # Validate entire batch first; never accept only one side or revisions.
        common=b.index.intersection(self.b.index)
        if len(common) and (not np.allclose(b.loc[common].to_numpy(),self.b.loc[common].to_numpy(),rtol=0,atol=1e-12) or not np.allclose(a.loc[common].to_numpy(),self.a.loc[common].to_numpy(),rtol=0,atol=1e-12)):
            self.error('CLOSED_BAR_REVISION_REFUSED',now,True);return
        new=b.loc[b.index.difference(self.b.index)]
        if len(new) and new.index[0]<self.b.index[-1]:self.error('LATE_BACKFILL_REFUSED',now,True);return
        self.ticks(ticks,now)
        for t,br in new.iterrows():
            prev=self.b.index[-1]
            if t-prev>BAR:self.emit('DATA_GAP',ts(prev)+'|'+ts(t),t,now,{'reason':'M5_GAP_NOT_FILLED','previous':ts(prev),'next':ts(t),'missing_slots':int((t-prev)/BAR)-1})
            payload={'time':ts(t),'bid':{k:float(br[k]) for k in ['open','high','low','close']},'ask':{k:float(a.loc[t,k]) for k in ['open','high','low','close']}}
            self.emit('BAR',ts(t),t+BAR,now,payload)
            self.b.loc[t]=br;self.a.loc[t]=a.loc[t]
        # Deadline rejection also happens when the source returns no fresh ticks.
        for key,s in list(self.pending.items()):
            if now>utc(s['entry_due_utc'])+pd.Timedelta(seconds=self.config['max_entry_lateness_seconds']):self.reject(s,'NO_FRESH_QUOTE_BY_DEADLINE',now)
        if self.b.index[-1]<=self.last_processed:return
        d,snaps,h,levels,signals,diagnostics=self.model_fn(self.b,self.a)
        for snap in snaps:
            if not self.start<=snap['known_at']<=now:continue
            for lev in snap['levels']:
                for label,known in [('seed',lev['created_at']),('important',lev['reactions'][1]['known_at'] if len(lev['reactions'])>=2 else None)]:
                    if known and self.start<=utc(known)<=now:
                        eid=event_id('D1_ZONE',lev['id']+'|'+label)
                        if eid not in self.journal.ids:self.emit('D1_ZONE',lev['id']+'|'+label,known,now,{'D1_level':lev['anchor'],'zone_id':lev['id'],'stage':label,'lower':lev['anchor']-lev['tol'],'upper':lev['anchor']+lev['tol'],'known_utc':known})
        for lev in levels:
            invalid=lev.get('invalidated_at')
            if invalid and self.start<=utc(invalid)<=now and event_id('D1_ZONE',lev['id']+'|invalidated') not in self.journal.ids:
                self.emit('D1_ZONE',lev['id']+'|invalidated',invalid,now,{'D1_level':lev['anchor'],'zone_id':lev['id'],'stage':'invalidated','reason':'ORIGINAL_D1_CLOSE_OR_AGE_RULE'})
        for c,state,counts,stop,has_signal in diagnostics:
            ck=model.context_key(c);confirm=utc(c['h1_confirm']);expires=confirm+pd.Timedelta(hours=6)
            if confirm>=self.start and confirm<=now and event_id('H1_CONTEXT',ck) not in self.journal.ids:self.emit('H1_CONTEXT',ck,confirm,now,{'direction':'BUY' if c['side']==1 else 'SELL','D1_level':c['level']['anchor'],'zone_id':c['level']['id'],'h1_open':c['h1_open'],'known_utc':c['h1_confirm']})
            if state:
                known=self.b.index[state['return_i']]+BAR
                if known>=self.start and event_id('M5_SWEEP_RETURN',ck) not in self.journal.ids:self.emit('M5_SWEEP_RETURN',ck,known,now,{'D1_level':c['level']['anchor'],'sweep_level':state['sweep_level'],'fixed_BOS':state['bos_level'],'known_utc':ts(known)})
            if state and not has_signal and self.b.index[state['return_i']]+BAR>=self.start and now<expires and event_id('M5_WAIT',ck) not in self.journal.ids:
                self.emit('M5_WAIT',ck,self.b.index[state['return_i']]+BAR,now,{'reason':'WAITING_FOR_FIXED_BOS','context':ck,'fixed_BOS':state['bos_level'],'expires_utc':ts(expires)})
            if not has_signal and expires>=self.start and (now>=expires or stop=='GAP' or (stop=='MISSING_FIRST_M5' and self.b.index[-1]+BAR>=confirm+BAR)):
                reason=('BOS_REJECTED_D1' if counts['BOS_rejected_D1'] else 'NO_BOS_AFTER_RETURN') if state else ('NO_SAME_BAR_RETURN' if counts['sweeps'] else 'NO_QUALIFIED_SWEEP')
                if event_id('REJECTION','context|'+ck) not in self.journal.ids:self.emit('REJECTION','context|'+ck,min(now,expires),now,{'reason':reason,'termination':stop if stop!='END' else 'TTL','context':ck,**counts})
        # Hourly gate statistics (not a list of hypothetical unformed signals).
        for i in range(1,len(h)):
            known=h.index[i]+pd.Timedelta(hours=1)
            if known<self.start or known>now:continue
            reason,counts=model.hour_diagnostic(h,i,snaps,[c for c,_,_,_,_ in diagnostics])
            if event_id('H1_FILTERS',ts(known)) not in self.journal.ids:self.emit('H1_FILTERS',ts(known),known,now,{'reason':reason,'gate_counts':counts})
            if reason!='H1_CONTEXT_CONFIRMED' and event_id('REJECTION','hour|'+ts(known)) not in self.journal.ids:self.emit('REJECTION','hour|'+ts(known),known,now,{'reason':reason,'gate_counts':counts,'unit':'hour_diagnostic_not_formed_signal'})
        for s in sorted(signals,key=lambda x:x['signal_time']):
            t=utc(s['signal_time'])
            if t>now or self.b.index[s['bos_i']]<self.start:continue
            sid=event_id('signal',model.context_key(s))
            if event_id('SIGNAL',sid) in self.journal.ids:continue
            side=s['side'];sl=float(self.b.low.iloc[s['sweep_i']] if side==1 else self.a.high.iloc[s['sweep_i']]);quote=s['candidate_quote'];due=t+BAR
            payload={'signal_id':sid,'direction':s['direction'],'side':side,'signal_utc':ts(t),'D1_level':s['level']['anchor'],'zone_id':s['level']['id'],'lower':s['level']['anchor']-s['level']['tol'],'upper':s['level']['anchor']+s['level']['tol'],'entry':quote,'entry_kind':'INDICATIVE_SIGNAL_CLOSE_NOT_FILL','SL':sl,'TP':quote+2*(quote-sl),'spread_pips':s['spread_pips'],'entry_due_utc':ts(due),'fixed_BOS':s['bos_level'],'signal_received_utc':ts(now)}
            self.emit('SIGNAL',sid,t,now,payload);self.pending[sid]=payload
            if now>=due:self.reject(payload,'SIGNAL_RECEIVED_TOO_LATE_NO_RETRO_ENTRY',now)
            elif self.position:self.reject(payload,'ONE_POSITION_ONLY',now)

        self.emit('ANALYSIS_CHECKPOINT',ts(self.b.index[-1]),self.b.index[-1]+BAR,now,{'bar_open_utc':ts(self.b.index[-1])})
        self.last_processed=self.b.index[-1]
