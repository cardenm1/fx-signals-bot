"""Offline fault injection and request coverage/calendar regressions."""
import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
from .test_watch import NOW,history,one_signal,no_model,ticks,rows
from .engine import Engine,initialize,BAR
from .ledger import Ledger
from .feed import from_ticks
from .cli import poll
from .market import is_open,expected_closed

class Recovery(unittest.TestCase):
    def test_crashes_before_and_after_each_commit(self):
        # Includes BAR, SIGNAL and checkpoint commits, and projection failure.
        for kind in ['BAR','SIGNAL','ANALYSIS_CHECKPOINT']:
            for after in [False,True]:
                for restart in [False,True]:
                    with self.subTest(kind=kind,after=after,restart=restart),tempfile.TemporaryDirectory() as tmp:
                        b,a=history();initialize(tmp,b,a,NOW);j=Ledger(tmp);e=Engine(j,one_signal)
                        nb=b.iloc[:1].copy();nb.index=pd.DatetimeIndex([NOW]);na=nb+.0001
                        original=j.append;fired=[]
                        def fail(k,*args,**kwargs):
                            if k==kind and not fired:
                                fired.append(True)
                                if after:original(k,*args,**kwargs)
                                raise RuntimeError('injected crash')
                            return original(k,*args,**kwargs)
                        with patch.object(j,'append',side_effect=fail):
                            with self.assertRaises(RuntimeError):e.ingest(nb,na,None,NOW+BAR+pd.Timedelta(seconds=3))
                        if restart:j.close();j=Ledger(tmp);e=Engine(j,one_signal)
                        e.ingest(nb,na,None,NOW+BAR+pd.Timedelta(seconds=4))
                        e.ingest(nb,na,None,NOW+BAR+pd.Timedelta(seconds=5))
                        self.assertEqual(len(rows(j,'BAR')),1);self.assertEqual(len(rows(j,'SIGNAL')),1)
                        self.assertEqual(len(rows(j,'ANALYSIS_CHECKPOINT')),1);self.assertEqual(e.last_processed,NOW)
                        self.assertFalse(rows(j,'ENTRY'));j.close()
    def test_model_failure_retries_without_new_bars(self):
        with tempfile.TemporaryDirectory() as tmp:
            b,a=history();initialize(tmp,b,a,NOW)
            with Ledger(tmp) as j:
                e=Engine(j,lambda b,a:(_ for _ in ()).throw(RuntimeError('model')))
                nb=b.iloc[:1].copy();nb.index=pd.DatetimeIndex([NOW])
                with self.assertRaises(RuntimeError):e.ingest(nb,nb+.0001,None,NOW+BAR+pd.Timedelta(seconds=3))
                e=Engine(j,one_signal);e.ingest(nb.iloc[:0],nb.iloc[:0],None,NOW+BAR+pd.Timedelta(seconds=4))
                self.assertEqual(len(rows(j,'SIGNAL')),1)
    def test_late_signal_commit_is_rejected_after_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            b,a=history();initialize(tmp,b,a,NOW)
            with Ledger(tmp) as j:
                e=Engine(j,one_signal);nb=b.iloc[:1].copy();nb.index=pd.DatetimeIndex([NOW]);original=j.append
                def fail(k,*args,**kw):
                    result=original(k,*args,**kw)
                    if k=='SIGNAL':raise RuntimeError('after signal')
                    return result
                with patch.object(j,'append',side_effect=fail):
                    with self.assertRaises(RuntimeError):e.ingest(nb,nb+.0001,None,NOW+2*BAR)
                e=Engine(j,one_signal);e.ingest(nb,nb+.0001,ticks(NOW+2*BAR),NOW+2*BAR)
                self.assertFalse(rows(j,'ENTRY'));self.assertEqual(len(rows(j,'REJECTION')),1)

class Coverage(unittest.TestCase):
    def raw(self):
        return pd.DataFrame({'bidPrice':[1.25,1.251,1.252],'askPrice':[1.2501,1.2511,1.2521]},index=pd.DatetimeIndex([NOW+pd.Timedelta(seconds=1),NOW+BAR,NOW+2*BAR-pd.Timedelta(seconds=1)]))
    def test_leading_partial_excluded(self):
        b,a,_=from_ticks(self.raw(),NOW+2*BAR+pd.Timedelta(seconds=3),request_start=NOW+pd.Timedelta(seconds=1),request_end=NOW+2*BAR)
        self.assertEqual(list(b.index),[NOW+BAR]);self.assertEqual(list(a.index),[NOW+BAR])
    def test_request_end_before_close_cannot_be_promoted_on_arrival(self):
        raw=self.raw().iloc[:1]
        b,_,_=from_ticks(raw,NOW+2*BAR,request_start=NOW,request_end=NOW+BAR-pd.Timedelta(seconds=1));self.assertTrue(b.empty)
    def test_missing_coverage_refused(self):
        with self.assertRaises(ValueError):from_ticks(self.raw(),NOW+2*BAR)
    def test_naive_coverage_refused(self):
        with self.assertRaises(ValueError):from_ticks(self.raw(),NOW+2*BAR,request_start=NOW.tz_localize(None),request_end=NOW+2*BAR)
    def test_aware_timezone_normalized_UTC(self):
        raw=self.raw();raw.index=raw.index.tz_convert('Europe/London')
        b,_,_=from_ticks(raw,NOW+2*BAR+pd.Timedelta(seconds=3),request_start=NOW,request_end=NOW+2*BAR)
        self.assertEqual(str(b.index.tz),'UTC')
    def test_crossed_forming_tick_refused(self):
        raw=self.raw();raw.iloc[-1,1]=1.0
        with self.assertRaises(ValueError):from_ticks(raw,NOW+2*BAR,request_start=NOW,request_end=NOW+2*BAR)

class Calendar(unittest.TestCase):
    def test_summer_winter_and_DST_weekends(self):
        for close,opening in [('2026-10-23T21:00Z','2026-10-25T21:00Z'),('2026-10-30T21:00Z','2026-11-01T22:00Z'),('2026-03-06T22:00Z','2026-03-08T21:00Z')]:
            c,o=pd.Timestamp(close),pd.Timestamp(opening)
            self.assertTrue(is_open(c-pd.Timedelta(seconds=1)));self.assertFalse(is_open(c))
            self.assertFalse(is_open(o-pd.Timedelta(seconds=1)));self.assertTrue(is_open(o))
            self.assertTrue(expected_closed(c,o-pd.Timedelta(seconds=1)))
            self.assertFalse(expected_closed(c-BAR,o-pd.Timedelta(seconds=1)))
    def test_closed_empty_separate_but_connection_errors_still_errors(self):
        close=pd.Timestamp('2030-01-11T22:00Z');b,a=history();b.index=pd.date_range(close-pd.Timedelta(hours=1),periods=12,freq='5min');a.index=b.index
        with tempfile.TemporaryDirectory() as tmp:
            initialize(tmp,b,a,close)
            with Ledger(tmp) as j:
                e=Engine(j,no_model);clock=lambda:close+pd.Timedelta(hours=1)
                poll(e,j,clock=clock,fetcher=lambda *a:pd.DataFrame())
                self.assertEqual(len(rows(j,'MARKET_CLOSED')),1);self.assertFalse(rows(j,'ERROR'))
                self.assertTrue(Path(tmp,'market.csv').exists())
                def fail(*a):raise TimeoutError('offline fixture')
                poll(e,j,clock=clock,fetcher=fail);self.assertEqual(len(rows(j,'ERROR')),1)
    def test_empty_active_market_remains_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            b,a=history();initialize(tmp,b,a,NOW)
            with Ledger(tmp) as j:
                poll(Engine(j,no_model),j,clock=lambda:NOW+BAR,fetcher=lambda *a:pd.DataFrame())
                self.assertEqual(len(rows(j,'ERROR')),1);self.assertFalse(rows(j,'MARKET_CLOSED'))

class ExecutionCommitRecovery(unittest.TestCase):
    def test_entry_exit_commits_not_duplicated(self):
        for stage in ['ENTRY','EXIT']:
            for after in [False,True]:
                with self.subTest(stage=stage,after=after),tempfile.TemporaryDirectory() as tmp:
                    b,a=history();initialize(tmp,b,a,NOW);j=Ledger(tmp);e=Engine(j,one_signal)
                    nb=b.iloc[:1].copy();nb.index=pd.DatetimeIndex([NOW]);e.ingest(nb,nb+.0001,None,NOW+BAR+pd.Timedelta(seconds=3))
                    when=NOW+2*BAR+pd.Timedelta(seconds=2);quote=ticks(when)
                    if stage=='EXIT':
                        e.ingest(nb.iloc[:0],nb.iloc[:0],quote,when);when+=pd.Timedelta(seconds=5)
                        quote=ticks(when,e.position['TP'],e.position['TP']+.0001)
                    original=j.append
                    def fail(k,*args,**kwargs):
                        if k==stage:
                            if after:original(k,*args,**kwargs)
                            raise RuntimeError('execution commit crash')
                        return original(k,*args,**kwargs)
                    with patch.object(j,'append',side_effect=fail):
                        with self.assertRaises(RuntimeError):e.ingest(nb.iloc[:0],nb.iloc[:0],quote,when)
                    j.close();j=Ledger(tmp);e=Engine(j,one_signal)
                    e.ingest(nb.iloc[:0],nb.iloc[:0],quote,when)
                    e.ingest(nb.iloc[:0],nb.iloc[:0],quote,when)
                    self.assertEqual(len(rows(j,'ENTRY')),1)
                    self.assertEqual(len(rows(j,'EXIT')),int(stage=='EXIT'));j.close()
    def test_projection_failure_after_signal_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            b,a=history();initialize(tmp,b,a,NOW);j=Ledger(tmp);e=Engine(j,one_signal)
            nb=b.iloc[:1].copy();nb.index=pd.DatetimeIndex([NOW]);original=j.sync_views
            def fail():
                if rows(j,'SIGNAL'):raise RuntimeError('projection failure')
                return original()
            with patch.object(j,'sync_views',side_effect=fail):
                with self.assertRaises(RuntimeError):e.ingest(nb,nb+.0001,None,NOW+BAR+pd.Timedelta(seconds=3))
            j.close()
            with Ledger(tmp) as j:
                prefix=Path(tmp,'signals.csv').read_bytes();e=Engine(j,one_signal)
                e.ingest(nb,nb+.0001,None,NOW+BAR+pd.Timedelta(seconds=4))
                self.assertEqual(len(rows(j,'SIGNAL')),1);self.assertEqual(Path(tmp,'signals.csv').read_bytes(),prefix)

class DurableWriteBoundary(unittest.TestCase):
    def test_disk_commit_before_in_memory_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            b,a=history();initialize(tmp,b,a,NOW)
            with Ledger(tmp) as j:
                e=Engine(j,one_signal);original=j.write_row;nb=b.iloc[:1].copy();nb.index=pd.DatetimeIndex([NOW]);fired=[]
                def fail(path,fields,row):
                    original(path,fields,row)
                    if path.name=='events.csv' and row['kind']=='BAR' and not fired:
                        fired.append(True);raise RuntimeError('disk commit before memory update')
                with patch.object(j,'write_row',side_effect=fail):
                    with self.assertRaises(RuntimeError):e.ingest(nb,nb+.0001,None,NOW+BAR+pd.Timedelta(seconds=3))
                e.ingest(nb,nb+.0001,None,NOW+BAR+pd.Timedelta(seconds=4))
                self.assertEqual(len(rows(j,'BAR')),1);self.assertEqual(len(rows(j,'SIGNAL')),1)
                self.assertEqual(len(j.read()),len(j.events))

class OfflineResume(unittest.TestCase):
    def test_saved_analysis_completed_before_failed_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            b,a=history();initialize(tmp,b,a,NOW)
            with Ledger(tmp) as j:
                e=Engine(j,lambda b,a:(_ for _ in ()).throw(RuntimeError('crash')))
                nb=b.iloc[:1].copy();nb.index=pd.DatetimeIndex([NOW])
                with self.assertRaises(RuntimeError):e.ingest(nb,nb+.0001,None,NOW+BAR+pd.Timedelta(seconds=3))
                e=Engine(j,one_signal)
                def fail(*a):raise ConnectionError('offline fixture')
                poll(e,j,clock=lambda:NOW+BAR+pd.Timedelta(seconds=4),fetcher=fail)
                self.assertEqual(e.last_processed,NOW);self.assertEqual(len(rows(j,'SIGNAL')),1)
                self.assertFalse(rows(j,'ENTRY'));self.assertEqual(len(rows(j,'ERROR')),1)
