"""All operational commands require --approve-paper. Tests never call network."""
import argparse,hashlib,json,sys,time
from pathlib import Path
import pandas as pd
from .engine import Engine,initialize,read_pair,utc,ts,BAR,validate_pair
from .ledger import Ledger,IntegrityError
from . import model

def now():return pd.Timestamp.now(tz='UTC')
def parser():
    p=argparse.ArgumentParser(description='GBPUSD PAPER ONLY. No broker, Telegram or publication commands.')
    sub=p.add_subparsers(dest='command',required=True)
    for cmd in ['init','once','run','bootstrap']:
        q=sub.add_parser(cmd);q.add_argument('--approve-paper',action='store_true',help='Only use after the user authorizes launch')
        if cmd!='bootstrap':q.add_argument('--session',required=True)
        if cmd in ['init','bootstrap']:q.add_argument('--bid',required=True);q.add_argument('--ask',required=True)
        if cmd=='init':q.add_argument('--slippage-pips',type=float,default=0)
        if cmd=='run':q.add_argument('--poll-seconds',type=float,default=15)
        if cmd=='bootstrap':q.add_argument('--out',required=True)
    for cmd in ['status','verify']:
        q=sub.add_parser(cmd);q.add_argument('--session',required=True)
    return p

def poll(engine,journal,clock=now,fetcher=None):
    from .feed import fetch_bounded,from_ticks,archive_raw
    fetcher=fetcher or fetch_bounded;t=clock();start=engine.b.index[-1]+BAR
    # Re-fetch last fully accepted M5 too: provider revisions halt the run.
    start-=BAR
    try:
        # Finish durable, unprocessed bars even if the next network request fails.
        if getattr(engine,'_needs_restore',False) or engine.b.index[-1]>engine.last_processed:
            engine.ingest(engine.b.iloc[:0],engine.a.iloc[:0],None,t)
        raw=fetcher(start,t)
        received=clock()
        if raw is None or raw.empty:
            from .market import expected_closed
            if expected_closed(engine.b.index[-1]+BAR,t):
                engine.emit('MARKET_CLOSED',ts(t),t,received,{'reason':'EXPECTED_WEEKEND_NY_17','next_unseen_utc':ts(engine.b.index[-1]+BAR),'calendar':'America/New_York DST'})
            else:engine.error('EMPTY_TICK_RESPONSE',received)
            empty=engine.b.iloc[:0];engine.ingest(empty,engine.a.iloc[:0],None,received)
        else:
            archive=archive_raw(journal.root,raw,start,t);engine.emit('SOURCE_BATCH',archive['sha256']+'|'+ts(t),received,received,archive)
            b,a,ticks=from_ticks(raw,received,engine.config['bar_finality_seconds'],request_start=start,request_end=t);engine.ingest(b,a,ticks,received)
        engine.emit('HEARTBEAT',ts(received),received,received,{'bars':len(engine.b),'pending':len(engine.pending),'open_position':engine.position['signal_id'] if engine.position else None,'halted':engine.halted})
    except IntegrityError:raise
    except Exception as exc:engine.error(type(exc).__name__+': '+str(exc),clock())

def bootstrap(args):
    from .feed import fetch_bounded
    b,a=read_pair(args.bid,args.ask);end=now();start=b.index[-1]+BAR;out=Path(args.out)
    if out.exists() and any(out.iterdir()):raise ValueError('Bootstrap output must be empty')
    frames=[]
    for side,old in [('BID',b),('ASK',a)]:
        raw=fetch_bounded(start,end,interval='INTERVAL_MIN_5',side='OFFER_SIDE_'+side,timeout=120)
        if raw.index.tz is None:raise ValueError('Naive SDK bar timestamps')
        extra=raw[['open','high','low','close']].astype(float);extra=extra[(extra.index>=start)&(extra.index+BAR<=end-pd.Timedelta(seconds=2))]
        frames.append(pd.concat([old,extra]))
    validate_pair(*frames);out.mkdir(parents=True,exist_ok=True)
    for side,frame in zip(['BID','ASK'],frames):
        with (out/f'GBPUSD_{side}_M5.csv').open('x') as f:frame.to_csv(f,index_label='timestamp')
    (out/'provenance.json').write_text(json.dumps({'source':'dukascopy-python4.0.1 historical M5; used ONLY as warmup, never scored','original_hashes':{s:hashlib.sha256(Path(p).read_bytes()).hexdigest() for s,p in [('BID',args.bid),('ASK',args.ask)]},'fetch_from':ts(start),'fetch_until':ts(end),'last_bar':ts(frames[0].index[-1]),'frozen':model.check_model()},indent=2))
    print('Warmup prepared. No paper session initialized or signals evaluated:',out)

def main(argv=None):
    p=parser();args=p.parse_args(argv)
    if args.command in ['init','once','run','bootstrap'] and not args.approve_paper:p.error('Not launched. User approval required before --approve-paper; no data fetched.')
    if args.command=='bootstrap':bootstrap(args);return
    if args.command=='init':
        b,a=read_pair(args.bid,args.ask);print(json.dumps(initialize(args.session,b,a,now(),args.slippage_pips),indent=2));return
    if not (Path(args.session)/'events.csv').exists():p.error('No initialized session (no directory created)')
    if args.command=='run' and args.poll_seconds<5:p.error('Poll interval must be >=5 seconds')
    with Ledger(args.session) as journal:
        engine=Engine(journal)
        if args.command in ['status','verify']:
            # Ledger verifies chain and existing projections on open, completing
            # interrupted projection appends without altering prior records.
            print(json.dumps({'mode':'PAPER_ONLY','events':len(journal.events),'evaluation_start':ts(engine.start),'pending':len(engine.pending),'position':engine.position,'halted':engine.halted,'last_processed_bar':ts(engine.last_processed),'last_tick':str(engine.last_tick),'publication':False},indent=2));return
        while True:
            if engine.halted:raise RuntimeError('HALTED: inspect errors.csv, no automatic unhalt')
            poll(engine,journal)
            if args.command=='once':break
            time.sleep(args.poll_seconds)

if __name__=='__main__':main()
