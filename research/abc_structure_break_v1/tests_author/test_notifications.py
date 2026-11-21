import argparse
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
import pandas as pd
from abc_author.notify import connect,deliver_once,run

S=dict(signal_id='unique',direction='long',variant='no_update',signal_time='2026-01-05T12:00:00Z',point2=1.2,stop=1.1,zone_low=1.1,zone_high=1.2)

class NotificationTests(unittest.TestCase):
    def test_persistent_dedup(self):
        with tempfile.TemporaryDirectory() as d:
            path=str(Path(d)/'state.sqlite'); calls=[]
            db=connect(path)
            self.assertTrue(deliver_once(db,S,lambda text:calls.append(text) or 1));db.close()
            db=connect(path)
            self.assertFalse(deliver_once(db,S,lambda text:calls.append(text) or 2))
            self.assertEqual(len(calls),1);db.close()

    def test_uncertain_never_retried(self):
        db=connect(':memory:')
        def fail(text):raise RuntimeError('timeout')
        with self.assertRaises(RuntimeError):deliver_once(db,S,fail)
        self.assertEqual(db.execute('SELECT status FROM delivery').fetchone()[0],'uncertain')
        self.assertFalse(deliver_once(db,S,lambda text:1));db.close()

    def test_stale_feed_cannot_send(self):
        args=argparse.Namespace(command='scan',bid='abc_author_data/GBPUSD_BID_M5_CLEAN.csv',ask='abc_author_data/GBPUSD_ASK_M5_CLEAN.csv',send=True,state='unused',initialize=True)
        stale=pd.DataFrame({'bid_close':[1.1]},index=pd.DatetimeIndex(['2026-01-05T11:55:00Z']))
        with patch('abc_author.notify.load',return_value=stale),patch('abc_author.notify.send') as sender:
            with self.assertRaisesRegex(ValueError,'Stale'):
                run(args,now='2026-10-25T12:00:00Z')
            sender.assert_not_called()

    def test_initialization_suppresses_history(self):
        f=pd.DataFrame({'bid_close':[1.1]},index=pd.DatetimeIndex(['2026-01-05T11:55:00Z']))
        with tempfile.TemporaryDirectory() as d:
            args=argparse.Namespace(command='scan',bid='',ask='',send=True,state=str(Path(d)/'state.sqlite'),initialize=True)
            with patch('abc_author.notify.load',return_value=f),patch('abc_author.notify.signals',return_value=[S]),patch('abc_author.notify.deliver_once') as delivery:
                run(args,now='2026-01-05T12:00:00Z')
                delivery.assert_not_called()
            db=connect(args.state)
            self.assertEqual(db.execute('SELECT status FROM delivery').fetchone()[0],'baseline');db.close()

if __name__=='__main__':unittest.main()

class CheckpointTests(unittest.TestCase):
    def test_remote_reservation_before_delivery(self):
        db=connect(':memory:');events=[]
        deliver_once(db,S,lambda text:events.append('send') or 1,
                     lambda:events.append('checkpoint'))
        self.assertEqual(events,['checkpoint','send','checkpoint']);db.close()

    def test_checkpoint_failure_blocks_send(self):
        db=connect(':memory:');calls=[]
        def fail():raise RuntimeError('remote unavailable')
        with self.assertRaises(RuntimeError):
            deliver_once(db,S,lambda text:calls.append(text),fail)
        self.assertEqual(calls,[])
        self.assertFalse(deliver_once(db,S,lambda text:calls.append(text)))
        db.close()
