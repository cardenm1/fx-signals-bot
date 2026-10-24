"""Offline only. Temporary synthetic sessions; no market observer started."""
import csv,io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
from research.paper_watch.ledger import Ledger,IntegrityError,event_id
from research.paper_watch.engine import Engine,initialize,validate_pair,read_pair,utc,ts,BAR
from research.paper_watch.feed import from_ticks
from research.paper_watch.cli import main,poll
from research.paper_watch import model

NOW=pd.Timestamp('2030-01-07T10:00:00Z')
def history():
    ix=pd.date_range(NOW-pd.Timedelta(hours=1),periods=12,freq='5min')
    b=pd.DataFrame({'open':1.25,'high':1.251,'low':1.249,'close':1.25},index=ix);return b,b+.0001

def no_model(b,a):return None,[],pd.DataFrame(),[],[],[]
def one_signal(b,a):
    i=len(b)-1;t=b.index[i]+BAR
    s={'signal_time':str(t),'bos_i':i,'sweep_i':i,'side':1,'direction':'BUY','candidate_quote':float(a.close.iloc[i]),'spread_pips':1.,'bos_level':1.25,'level':{'id':'L001','anchor':1.25,'tol':.001},'h1_open':str(b.index[i].floor('h'))}
    return None,[],pd.DataFrame(),[],[s],[]

def ticks(t,bid=1.25,ask=1.2501):return pd.DataFrame({'bid':[bid],'ask':[ask]},index=pd.DatetimeIndex([t]))
def rows(j,kind):return [json.loads(x['payload_json']) for x in j.events if x['kind']==kind]

class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'run';b,a=history();initialize(self.root,b,a,NOW)
        self.j=Ledger(self.root);self.e=Engine(self.j,one_signal)
    def tearDown(self):self.j.close();self.tmp.cleanup()
    def newbar(self,now=None):
        b,a=history();b=b.iloc[:1].copy();b.index=pd.DatetimeIndex([NOW]);a=b+.0001
        self.e.ingest(b,a,ticks(NOW+BAR+pd.Timedelta(seconds=1)),now or NOW+BAR+pd.Timedelta(seconds=3))
    def quote(self,time,bid=1.25,ask=1.2501,observed=None):
        self.e.ingest(self.e.b.iloc[:0],self.e.a.iloc[:0],ticks(time,bid,ask),observed or time+pd.Timedelta(seconds=1))
    def enter(self):
        self.newbar();self.quote(NOW+2*BAR+pd.Timedelta(seconds=2));return rows(self.j,'ENTRY')[0]
    def test_initial_start_is_not_research_history(self):
        self.assertEqual(self.e.start,NOW);self.assertEqual(rows(self.j,'SIGNAL'),[])
    def test_signal_and_five_minute_delay(self):
        self.newbar();self.assertEqual(len(rows(self.j,'SIGNAL')),1);self.assertIsNone(self.e.position)
        self.quote(NOW+BAR+pd.Timedelta(seconds=30));self.assertIsNone(self.e.position)
        self.quote(NOW+2*BAR+pd.Timedelta(seconds=2));self.assertEqual(len(rows(self.j,'ENTRY')),1)
    def test_buy_enters_ask_tp_two_R(self):
        p=self.enter();self.assertEqual(p['entry'],1.2501);self.assertAlmostEqual(p['TP']-p['entry'],2*(p['entry']-p['SL']))
    def test_no_retroactive_fill_when_signal_arrives_late(self):
        self.newbar(NOW+3*BAR);self.assertEqual(rows(self.j,'ENTRY'),[]);self.assertEqual(rows(self.j,'REJECTION')[-1]['reason'],'SIGNAL_RECEIVED_TOO_LATE_NO_RETRO_ENTRY')
    def test_stale_quote_does_not_enter(self):
        self.newbar();self.quote(NOW+BAR+pd.Timedelta(seconds=1),observed=NOW+2*BAR+pd.Timedelta(seconds=5));self.assertEqual(rows(self.j,'ENTRY'),[])
    def test_pending_expires_without_quote(self):
        self.newbar();self.e.ingest(self.e.b.iloc[:0],self.e.a.iloc[:0],None,NOW+3*BAR);self.assertFalse(self.e.pending)
    def test_quote_before_due_cannot_enter(self):
        self.newbar();self.quote(NOW+2*BAR-pd.Timedelta(seconds=1),observed=NOW+2*BAR+pd.Timedelta(seconds=1));self.assertIsNone(self.e.position)
    def test_SL_uses_bid_and_gap_can_exceed_one_R(self):
        p=self.enter();self.quote(NOW+pd.Timedelta(hours=1),bid=p['SL']-.002,ask=p['SL']-.0019)
        x=rows(self.j,'EXIT')[-1];self.assertLess(x['R'],-1);self.assertTrue(x['gap_or_data_uncertainty']);self.assertEqual(x['exit'],p['SL']-.002)
    def test_TP_uses_limit_even_on_favourable_jump(self):
        p=self.enter();self.quote(NOW+2*BAR+pd.Timedelta(seconds=15),bid=p['TP']+.001,ask=p['TP']+.0011)
        x=rows(self.j,'EXIT')[-1];self.assertAlmostEqual(x['R'],2);self.assertEqual(x['exit'],p['TP'])
    def test_entry_slippage_applied(self):
        self.e.config['slippage_pips']=.5;self.newbar();self.quote(NOW+2*BAR+pd.Timedelta(seconds=2));self.assertAlmostEqual(rows(self.j,'ENTRY')[-1]['entry'],1.25015)
    def test_one_position_only(self):
        self.enter();s=rows(self.j,'SIGNAL')[0].copy();s['signal_id']='second';s['entry_due_utc']=ts(NOW+2*BAR)
        self.j.append('SIGNAL','second',ts(NOW+BAR),ts(NOW+BAR),s);self.e.pending['second']=s
        self.quote(NOW+2*BAR+pd.Timedelta(seconds=10));self.assertEqual(len(rows(self.j,'ENTRY')),1);self.assertEqual(rows(self.j,'REJECTION')[-1]['reason'],'ONE_POSITION_ONLY')
    def test_restart_restores_position(self):
        p=self.enter();self.j.close();self.j=Ledger(self.root);restored=Engine(self.j,no_model);self.assertEqual(restored.position,p)
    def test_duplicate_bar_and_signal_not_relogged(self):
        self.newbar();n=len(rows(self.j,'SIGNAL'));b=self.e.b.loc[[NOW]];a=self.e.a.loc[[NOW]]
        self.e.ingest(b,a,ticks(NOW+BAR+pd.Timedelta(seconds=2)),NOW+BAR+pd.Timedelta(seconds=4));self.assertEqual(len(rows(self.j,'SIGNAL')),n)
    def test_old_signal_excluded(self):
        self.e.model_fn=lambda b,a:one_signal(b.iloc[:12],a.iloc[:12]);self.newbar();self.assertEqual(rows(self.j,'SIGNAL'),[])
    def test_no_signal_journal_edit_after_entry(self):
        self.newbar();prefix=(self.root/'signals.csv').read_bytes();self.quote(NOW+2*BAR+pd.Timedelta(seconds=2));self.assertEqual((self.root/'signals.csv').read_bytes(),prefix)
    def test_revision_halts_without_replacing_bar(self):
        self.newbar();old=self.e.b.loc[NOW,'close'];b=self.e.b.loc[[NOW]].copy();a=self.e.a.loc[[NOW]].copy();b.loc[NOW,'close']=old-.0001
        self.e.ingest(b,a,None,NOW+2*BAR);self.assertTrue(self.e.halted);self.assertEqual(self.e.b.loc[NOW,'close'],old)
    def test_backfill_halts(self):
        self.newbar();b=self.e.b.iloc[:1].copy();b.index=pd.DatetimeIndex([NOW-pd.Timedelta(minutes=90)]);a=b+.0001
        self.e.ingest(b,a,None,NOW+2*BAR);self.assertTrue(self.e.halted)
    def test_future_bar_rejected(self):
        b,a=history();b=b.iloc[:1].copy();b.index=pd.DatetimeIndex([NOW]);a=b+.0001
        with self.assertRaises(ValueError):self.e.ingest(b,a,None,NOW+pd.Timedelta(minutes=4))
    def test_future_tick_rejected(self):
        with self.assertRaises(ValueError):self.quote(NOW+BAR,observed=NOW)
    def test_fetch_error_logged_without_network(self):
        def fail(*args):raise TimeoutError('synthetic data error')
        poll(self.e,self.j,clock=lambda:NOW+BAR,fetcher=fail);self.assertTrue(rows(self.j,'ERROR'))
    def test_error_marks_open_trade_uncertain(self):
        p=self.enter();self.e.error('synthetic outage',NOW+2*BAR+pd.Timedelta(seconds=4));self.quote(NOW+2*BAR+pd.Timedelta(seconds=15),p['TP'],p['TP']+.0001)
        self.assertTrue(rows(self.j,'EXIT')[-1]['gap_or_data_uncertainty'])
    def test_sell_entry_bid_exit_ask(self):
        self.newbar();s=rows(self.j,'SIGNAL')[0].copy();self.e.pending.clear();s.update({'signal_id':'sell','side':-1,'direction':'SELL','SL':1.252})
        self.e.pending['sell']=s;self.quote(NOW+2*BAR+pd.Timedelta(seconds=2));p=rows(self.j,'ENTRY')[-1];self.assertEqual(p['entry'],1.25)
        self.quote(NOW+2*BAR+pd.Timedelta(seconds=15),1.2519,1.2521);self.assertEqual(rows(self.j,'EXIT')[-1]['exit'],1.2521)
    def test_entry_on_wrong_side_of_SL_rejected(self):
        self.newbar();self.quote(NOW+2*BAR+pd.Timedelta(seconds=2),1.248,1.2481);self.assertEqual(rows(self.j,'ENTRY'),[]);self.assertEqual(rows(self.j,'REJECTION')[-1]['reason'],'ENTRY_BEYOND_STRUCTURAL_SL')

class LedgerTests(unittest.TestCase):
    def test_idempotent_append_and_crash_projection_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            with Ledger(tmp) as j:
                j.append('SIGNAL','x',ts(NOW),ts(NOW),{'signal_id':'x','entry':1.2});j.append('SIGNAL','x',ts(NOW),ts(NOW),{'signal_id':'x','entry':1.2});self.assertEqual(len(j.events),1)
            Path(tmp,'signals.csv').unlink() # synthetic crash before view creation
            with Ledger(tmp) as j:self.assertEqual(len(j.events),1);self.assertTrue(Path(tmp,'signals.csv').exists())
    def test_changed_repeated_id_refused(self):
        with tempfile.TemporaryDirectory() as tmp,Ledger(tmp) as j:
            j.append('ERROR','x',ts(NOW),ts(NOW),{'reason':'a'})
            with self.assertRaises(IntegrityError):j.append('ERROR','x',ts(NOW),ts(NOW),{'reason':'b'})
    def test_second_writer_refused(self):
        with tempfile.TemporaryDirectory() as tmp,Ledger(tmp):
            with self.assertRaises(IntegrityError):Ledger(tmp)
    def test_hash_chain_detects_modification(self):
        with tempfile.TemporaryDirectory() as tmp:
            with Ledger(tmp) as j:j.append('ERROR','x',ts(NOW),ts(NOW),{'reason':'original'})
            p=Path(tmp,'events.csv');p.write_text(p.read_text().replace('original','changed'))
            with self.assertRaises(IntegrityError):Ledger(tmp)
    def test_view_modification_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with Ledger(tmp) as j:j.append('ERROR','x',ts(NOW),ts(NOW),{'reason':'original'})
            p=Path(tmp,'errors.csv');p.write_text(p.read_text().replace('original','changed'))
            with self.assertRaises(IntegrityError):Ledger(tmp)
    def test_partial_tail_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with Ledger(tmp) as j:j.append('ERROR','x',ts(NOW),ts(NOW),{'reason':'original'})
            with Path(tmp,'events.csv').open('ab') as f:f.write(b'partial')
            with self.assertRaises(IntegrityError):Ledger(tmp)

class InputTests(unittest.TestCase):
    def test_model_frozen_sha(self):
        self.assertEqual(model.check_model()['recognize.py'],'fa8c5d24d972316d885760f63ce91666f71f2fe10f3b8cc94d26679e4ca040b9')
    def test_naive_timestamp_refused(self):
        with self.assertRaises(ValueError):utc('2030-01-01')
    def test_misaligned_pair_refused(self):
        b,a=history()
        with self.assertRaises(ValueError):validate_pair(b,a.iloc[:-1])
    def test_crossed_spread_refused(self):
        b,a=history();a=b-.0001
        with self.assertRaises(ValueError):validate_pair(b,a)
    def test_nonfinite_prices_refused(self):
        b,a=history();b.iloc[0,0]=np.nan
        with self.assertRaises(ValueError):validate_pair(b,a)
    def test_duplicate_bar_refused(self):
        b,a=history()
        with self.assertRaises(ValueError):validate_pair(pd.concat([b,b.iloc[-1:]]),pd.concat([a,a.iloc[-1:]]))
    def test_tick_aggregation_no_incomplete_candles(self):
        ix=pd.DatetimeIndex([NOW,NOW+BAR-pd.Timedelta(milliseconds=1),NOW+BAR]);raw=pd.DataFrame({'bidPrice':[1.25,1.251,1.252],'askPrice':[1.2501,1.2511,1.2521]},index=ix)
        b,a,t=from_ticks(raw,NOW+BAR+pd.Timedelta(seconds=2),request_start=NOW,request_end=NOW+BAR+pd.Timedelta(seconds=2));self.assertEqual(len(b),1);self.assertEqual(b.iloc[0].close,1.251);self.assertEqual(len(t),3)
    def test_cli_requires_explicit_launch_flag(self):
        with tempfile.TemporaryDirectory() as tmp,patch('sys.stderr',new=io.StringIO()):
            with self.assertRaises(SystemExit):main(['once','--session',str(Path(tmp)/'not-created')])
            self.assertFalse(Path(tmp,'not-created').exists())
    def test_initialize_never_overwrites_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            b,a=history();initialize(Path(tmp)/'s',b,a,NOW)
            with self.assertRaises(ValueError):initialize(Path(tmp)/'s',b,a,NOW)

class ModelRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        folder=Path(__file__).resolve().parents[1]/'data'
        if not (folder/'GBPUSD_BID_M5_CLEAN.csv').exists():raise unittest.SkipTest('Optional local frozen-history parity data not included in source archive')
        cls.b,cls.a=read_pair(folder/'GBPUSD_BID_M5_CLEAN.csv',folder/'GBPUSD_ASK_M5_CLEAN.csv')
        cls.result=model.analyse(cls.b,cls.a)
    def test_five_original_signals_unchanged_no_execution(self):
        signals=self.result[4]
        self.assertEqual([s['signal_time'] for s in signals],['2026-03-03 14:45:00+00:00','2026-03-06 12:05:00+00:00','2026-04-13 22:05:00+00:00','2026-05-07 10:20:00+00:00','2026-05-08 15:25:00+00:00'])
        self.assertEqual(len(self.result[5]),15)
    def test_added_future_does_not_change_past_signals(self):
        cutoff=self.b.index[35999]+BAR;short=model.analyse(self.b.iloc[:36000],self.a.iloc[:36000])[4]
        key=lambda s:(s['signal_time'],s['level']['id'],s['bos_level'])
        self.assertEqual([key(s) for s in short],[key(s) for s in self.result[4] if utc(s['signal_time'])<=cutoff])

class LifecycleTests(unittest.TestCase):
    def test_unfinished_first_context_M5_is_not_premature_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            b,a=history();root=Path(tmp)/'run';initialize(root,b,a,NOW)
            with Ledger(root) as j:
                e=Engine(j,no_model)
                c={'h1_confirm':str(NOW+BAR),'h1_open':str(NOW),'side':1,'level':{'id':'L','anchor':1.25}}
                e.model_fn=lambda b,a:(None,[],pd.DataFrame(),[],[],[(c,None,{'sweeps':0,'no_same_bar_return':0},'MISSING_FIRST_M5',False)])
                nb=b.iloc[:1].copy();nb.index=pd.DatetimeIndex([NOW]);e.ingest(nb,nb+.0001,None,NOW+BAR+pd.Timedelta(seconds=3))
                self.assertEqual(rows(j,'REJECTION'),[])
    def test_runtime_change_refused_on_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            b,a=history();root=Path(tmp)/'run';initialize(root,b,a,NOW)
            with Ledger(root) as j,patch('research.paper_watch.engine.runtime_manifest',return_value={'changed':'yes'}):
                with self.assertRaises(ValueError):Engine(j,no_model)

class MoreReadinessTests(unittest.TestCase):
    def test_stale_warmup_requires_bridge(self):
        with tempfile.TemporaryDirectory() as tmp:
            b,a=history();b.index=b.index-pd.Timedelta(days=3);a.index=a.index-pd.Timedelta(days=3)
            with self.assertRaisesRegex(ValueError,'Warmup stale'):initialize(Path(tmp)/'run',b,a,NOW)
    def test_confirmation_journal_has_no_fill_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            b,a=history();root=Path(tmp)/'run';initialize(root,b,a,NOW)
            with Ledger(root) as j:
                e=Engine(j,one_signal);nb=b.iloc[:1].copy();nb.index=pd.DatetimeIndex([NOW]);e.ingest(nb,nb+.0001,None,NOW+BAR+pd.Timedelta(seconds=3))
                self.assertEqual(rows(j,'SIGNAL')[0]['entry_kind'],'INDICATIVE_SIGNAL_CLOSE_NOT_FILL');self.assertEqual(rows(j,'ENTRY'),[])

class HourlyParity(unittest.TestCase):
    def test_no_false_H1_break_from_diagnostic(self):
        ix=pd.date_range('2030-01-07T09:00Z',periods=2,freq='1h')
        h=pd.DataFrame({'valid':True,'atr_before':.001,'close':[1.24,1.24]},index=ix)
        snaps=[{'known_at':pd.Timestamp('2030-01-07T00:00Z'),'valid':True,'structure':1,'levels':[{'id':'L','type':'high','anchor':1.25,'tol':.001,'reactions':[{},{}],'age':10}]}]
        reason,counts=model.hour_diagnostic(h,1,snaps,[]);self.assertEqual(counts['NO_H1_CLOSE_BREAK_WITH_BUFFER'],1);self.assertEqual(reason,'NO_NEW_H1_CONTEXT')

class ArchiveIntegrityTests(unittest.TestCase):
    def test_source_archive_tampering_refuses_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            b,a=history();root=Path(tmp)/'run';initialize(root,b,a,NOW)
            with Ledger(root) as j:
                j.append('SOURCE_BATCH','fake',ts(NOW),ts(NOW),{'path':'raw/missing.csv','sha256':'0'*64})
                with self.assertRaises(IntegrityError):Engine(j,no_model)

class WarmupIntegrityTests(unittest.TestCase):
    def test_warmup_change_refused_on_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            b,a=history();root=Path(tmp)/'run';initialize(root,b,a,NOW)
            with (root/'warmup_BID.csv').open('a') as f:f.write('\n')
            with Ledger(root) as j:
                with self.assertRaises(IntegrityError):Engine(j,no_model)
