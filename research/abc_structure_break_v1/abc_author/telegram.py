"""Notification transport only. No broker or order API."""
import json
import os
import urllib.request


def send(text):
    token=os.environ.get('ABC_AUTHOR_TELEGRAM_BOT_TOKEN','')
    chat=os.environ.get('ABC_AUTHOR_TELEGRAM_CHAT_ID','')
    if not token or not chat:
        raise RuntimeError('Set ABC_AUTHOR_TELEGRAM_BOT_TOKEN and ABC_AUTHOR_TELEGRAM_CHAT_ID')
    payload=json.dumps({'chat_id':chat,'text':text}).encode()
    req=urllib.request.Request('https://api.telegram.org/bot'+token+'/sendMessage',
                               data=payload,headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=20) as response:
            result=json.load(response)
        if result.get('ok') is not True:
            raise ValueError('Telegram API rejected message')
        return result['result']['message_id']
    except Exception:
        # Never expose request URLs containing the token, including in tracebacks.
        raise RuntimeError('Telegram delivery failed or uncertain; no automatic retry') from None


def format_signal(s):
    return (f"ABC Structure Break author r2 | GBP/USD | DEMO OBSERVATION\n"
            f"{s['direction']} | {s.get('pair','H1/M5')} | {s['variant']}\nClosed signal UTC: {s['signal_time']}\n"
            f"Point 2: {s['point2']:.5f}; research stop: {s['stop']:.5f}\n"
            f"HTF block: {s['zone_low']:.5f}–{s['zone_high']:.5f}\n"
            f"ID: {s['signal_id']}\nResearch assumptions; no order placed. "
            'Historical execution uses next consecutive M5 open plus adverse slippage, which a delayed alert cannot reproduce.')
