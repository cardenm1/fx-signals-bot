"""Opt-in PAPER notifications. Durable intent prevents automatic duplicate sends.
Telegram has no idempotency key: uncertain attempts require manual reconciliation.
"""
import argparse,json,os,re,urllib.request
from pathlib import Path
import pandas as pd
from .engine import utc,ts
from .ledger import Ledger,IntegrityError,event_id
from .github_runner import verify,clock

FRESHNESS=pd.Timedelta(minutes=5)
class PublishError(RuntimeError):pass

def format_signal(signal):
    return ('PAPER | DEMO | GBP/USD Strategy v1.0\n'
        'D1 → H1 → M5, строгая модель\n'
        f"{signal['direction']} | UTC: {signal['signal_utc']}\n"
        f"D1: {signal['D1_level']:.5f}\n"
        f"Вход (ориентир, не исполнение): {signal['entry']:.5f}\n"
        f"SL: {signal['SL']:.5f} | TP 2R: {signal['TP']:.5f}\n"
        f"Спред: {signal['spread_pips']:.1f} pip\n"
        f"ID: {signal['signal_id']}\n"
        'Только демонаблюдение. Сделка не открыта.')

def credentials():
    token=os.environ.get('TELEGRAM_BOT_TOKEN','');chat=os.environ.get('TELEGRAM_CHAT_ID','')
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+',token) or not chat:raise PublishError('Telegram secrets missing or invalid')
    return token,chat

def send_telegram(text):
    token,chat=credentials()
    req=urllib.request.Request('https://api.telegram.org/bot'+token+'/sendMessage',data=json.dumps({'chat_id':chat,'text':text,'disable_web_page_preview':True}).encode(),headers={'Content-Type':'application/json'},method='POST')
    try:
        with urllib.request.urlopen(req,timeout=15) as reply:result=json.load(reply)
        if result.get('ok') is not True:raise PublishError('Telegram rejected request')
        return result['result']['message_id']
    except Exception:raise PublishError('Telegram outcome unknown; no automatic retry') from None

def publish(root,persist,*,approved=False,sender=send_telegram,now_fn=clock):
    # Both the caller AND repository setting must explicitly enable publication.
    if not approved or os.environ.get('PAPER_WATCH_TELEGRAM_ENABLED')!='true':return {'status':'DISABLED','sent':0}
    root=Path(root);verify(root);now=utc(now_fn())
    with Ledger(root/'session') as source,Ledger(root/'session/notifications') as outbox:
        activation=next((r for r in outbox.events if r['kind']=='ACTIVATED'),None)
        if activation is None:
            outbox.append('ACTIVATED','v1',ts(now),ts(now),{'not_before_utc':ts(now),'source_head':source.events[-1]['hash']})
            persist()  # No pre-activation signals can be sent on first enable.
            return {'status':'ACTIVATED_NO_BACKLOG','sent':0}
        activated=utc(json.loads(activation['payload_json'])['not_before_utc']);sent=0
        results={json.loads(r['payload_json'])['signal_id']:json.loads(r['payload_json'])['status'] for r in outbox.events if r['kind']=='NOTIFY_RESULT'}
        unknown=sum(results.get(json.loads(r['payload_json'])['signal_id'])!='SENT' for r in outbox.events if r['kind']=='NOTIFY_INTENT')
        for row in source.events:
            if row['kind']!='SIGNAL':continue
            s=json.loads(row['payload_json']);sid=s['signal_id'];when=utc(s['signal_utc']);received=utc(s['signal_received_utc'])
            if when<=activated or received<activated or not pd.Timedelta(0)<=now-when<=FRESHNESS:continue
            if received>=utc(s['entry_due_utc']):continue  # Recovered historical context is never a fresh alert.
            if s['direction'] not in {'BUY','SELL'}:raise IntegrityError('Invalid signal direction')
            if event_id('NOTIFY_INTENT',sid) in outbox.ids:continue
            outbox.append('NOTIFY_INTENT',sid,ts(now),ts(now),{'signal_id':sid,'signal_utc':s['signal_utc'],'policy':'AT_MOST_ONE_ATTEMPT'})
            persist()  # Must be on remote branch BEFORE the network side effect.
            try:
                message_id=sender(format_signal(s));payload={'signal_id':sid,'status':'SENT','telegram_message_id':message_id};sent+=1
            except Exception:
                payload={'signal_id':sid,'status':'UNKNOWN_NO_RETRY'};unknown+=1
            outbox.append('NOTIFY_RESULT',sid,ts(now),ts(now),payload)
            persist()
        return {'status':'UNCERTAIN_DELIVERY' if unknown else 'OK','sent':sent,'unknown':unknown}

def main():
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--approve-publication',action='store_true');a=p.parse_args()
    from .state_store import GitStore
    root=Path(a.root)
    if a.approve_publication and os.environ.get('PAPER_WATCH_TELEGRAM_ENABLED')=='true':credentials()
    result=publish(root,GitStore(root).save,approved=a.approve_publication)
    print(json.dumps(result))
    if result['status']=='UNCERTAIN_DELIVERY':raise SystemExit(1)
if __name__=='__main__':main()
