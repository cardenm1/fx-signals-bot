import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import pandas as pd
from .data import load,STEP
from .engine import signals,Config
from .telegram import send,format_signal


def connect(path):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=30)
    db.execute('PRAGMA journal_mode=DELETE')
    db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS delivery (id TEXT PRIMARY KEY,status TEXT NOT NULL,message_id TEXT)')
    db.commit()
    return db


def deliver_once(db,s,sender=send,checkpoint=lambda:None):
    # Commit reservation BEFORE the external side effect. Crash/timeout leaves an
    # uncertain reservation. Prefer a missed alert to a duplicate; inspect manually.
    with db:
        cur=db.execute("INSERT OR IGNORE INTO delivery VALUES (?, 'reserved', NULL)",(s['signal_id'],))
    if cur.rowcount==0:return False
    checkpoint() # Durable remote reservation before Telegram; failure aborts send.
    try:
        mid=sender(format_signal(s))
    except Exception:
        with db:db.execute("UPDATE delivery SET status='uncertain' WHERE id=?",(s['signal_id'],))
        checkpoint()
        raise
    with db:db.execute("UPDATE delivery SET status='sent',message_id=? WHERE id=?",(str(mid),s['signal_id']))
    checkpoint()
    return True


def run(args,now=None):
    now=pd.Timestamp(now or datetime.now(timezone.utc))
    if args.command=='test-message':
        msg='ABC Structure Break author r2 | MANUAL TELEGRAM TEST | notifications only; no order placed.'
        if args.send: print('Telegram message_id:',send(msg))
        else:print(msg+' [DRY RUN]')
        return
    f=load(args.bid,args.ask,asof=now)
    if f.empty or now-(f.index[-1]+STEP)>pd.Timedelta(minutes=15):
        raise ValueError('Stale/empty M5 feed (>15 minutes). Refresh both BID/ASK CSVs; historical signals will not be sent.')
    ss=signals(f)+signals(f,Config(high_minutes=240,low_minutes=15))
    fresh=[s for s in ss if pd.Timedelta(0)<=now-pd.Timestamp(s['signal_time'])<=pd.Timedelta(minutes=15)]
    if not args.send:
        print(json.dumps({'dry_run':True,'recent_signals':fresh},indent=2));return
    if not args.state:raise ValueError('--state is required with --send')
    db=connect(args.state)
    checkpoint=(lambda: github_checkpoint(args.state)) if getattr(args,'checkpoint_github',False) else (lambda:None)
    try:
        initialized=db.execute("SELECT value FROM meta WHERE key='initialized_at'").fetchone()
        if not initialized:
            if not args.initialize:
                raise ValueError('First run requires --initialize; this seeds history WITHOUT sending signals')
            with db:
                db.executemany("INSERT OR IGNORE INTO delivery VALUES (?, 'baseline', NULL)",[(s['signal_id'],) for s in ss])
                db.execute("INSERT INTO meta VALUES ('initialized_at',?)",(str(now),))
            checkpoint()
            print('State initialized. Existing signals suppressed.');return
        if args.initialize:raise ValueError('Already initialized; use scan without --initialize')
        baseline=pd.Timestamp(initialized[0])
        for s in fresh:
            if pd.Timestamp(s['signal_time'])>baseline:
                print(s['signal_id'],'sent' if deliver_once(db,s,checkpoint=checkpoint) else 'already handled')
    finally:db.close()


def github_checkpoint(path):
    import base64
    import os
    import subprocess
    repo=os.environ['GITHUB_REPOSITORY']
    endpoint=f'repos/{repo}/contents/abc.sqlite'
    get=subprocess.run(['gh','api',endpoint+'?ref=abc-author-state'],capture_output=True,text=True)
    body={'message':'ABC notification state checkpoint','branch':'abc-author-state',
          'content':base64.b64encode(Path(path).read_bytes()).decode()}
    if get.returncode==0:
        body['sha']=json.loads(get.stdout)['sha']
    elif '404' not in get.stderr:
        raise RuntimeError('Cannot read dedicated GitHub state')
    put=subprocess.run(['gh','api','--method','PUT',endpoint,'--input','-'],
                       input=json.dumps(body),capture_output=True,text=True)
    if put.returncode:
        raise RuntimeError('Cannot persist GitHub state; sending aborted')


def main():
    p=argparse.ArgumentParser()
    p.add_argument('command',choices=['scan','test-message'])
    p.add_argument('--bid',default='abc_author_data/GBPUSD_BID_M5_CLEAN.csv')
    p.add_argument('--ask',default='abc_author_data/GBPUSD_ASK_M5_CLEAN.csv')
    p.add_argument('--state',default='abc_author_state/abc.sqlite')
    p.add_argument('--send',action='store_true')
    p.add_argument('--initialize',action='store_true')
    p.add_argument('--checkpoint-github',action='store_true',help='Persist reservations on dedicated abc-author-state branch before sending')
    args=p.parse_args()
    try:run(args)
    except Exception as exc:
        p.exit(1,str(exc)+'\n')

if __name__=='__main__':main()
