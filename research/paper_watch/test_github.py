"""Offline hosted-runner, durable-branch and Telegram fault integration tests."""
import json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
from .test_watch import history,NOW,no_model,one_signal,rows
from .engine import BAR,ts,read_pair
from .ledger import Ledger,IntegrityError
from .github_runner import seed,verify,observe,SignalLedger,release_hash
from . import github_runner as runner
from .state_store import GitStore,StoreError,BRANCH
from .telegram_outbox import publish,format_signal

GIT_FALLBACK=Path(sys.executable).resolve().parents[2]/'bin/fallback'

def bars_after(n=3):
    b,a=history();nb=b.iloc[:1].copy()
    nb=pd.concat([nb]*n);nb.index=pd.date_range(NOW,periods=n,freq='5min')
    return pd.concat([b,nb]),pd.concat([a,nb+.0001])

def raw_quotes(b,a):
    stamps=[];bid=[];ask=[]
    for t,row in b.iterrows():
        for key,second in [('open',0),('high',1),('low',2),('close',299)]:
            stamps.append(t+pd.Timedelta(seconds=second));bid.append(row[key]);ask.append(a.loc[t,key])
    return pd.DataFrame({'bidPrice':bid,'askPrice':ask},index=pd.DatetimeIndex(stamps))

def fetch_fixture(b,a):
    raw=raw_quotes(b,a)
    return lambda start,end:raw[(raw.index>=start)&(raw.index<=end)]

class HostedRunner(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'state';b,a=history();seed(self.root,b,a,NOW)
    def tearDown(self):self.tmp.cleanup()
    def test_three_complete_bars_restart_no_duplicate_processing(self):
        b,a=bars_after(3);fetch=fetch_fixture(b,a);when=NOW+3*BAR+pd.Timedelta(seconds=3)
        first=observe(self.root,fetch,lambda:when,one_signal)
        with Ledger(self.root/'session') as j:
            self.assertEqual(len(rows(j,'BAR')),3);self.assertEqual(len(rows(j,'SIGNAL')),1);self.assertFalse(rows(j,'ENTRY'));self.assertFalse(rows(j,'EXIT'))
            prefix=(self.root/'session/signals.csv').read_bytes()
        later=when+pd.Timedelta(seconds=1);observe(self.root,fetch,lambda:later,one_signal)
        with Ledger(self.root/'session') as j:
            self.assertEqual(len(rows(j,'BAR')),3);self.assertEqual(len(rows(j,'SIGNAL')),1);self.assertEqual(len(rows(j,'ANALYSIS_CHECKPOINT')),1)
        self.assertEqual((self.root/'session/signals.csv').read_bytes(),prefix)
        self.assertEqual(first['last_processed_M5'],ts(NOW+2*BAR))
    def test_short_disconnect_catchup(self):
        b,a=bars_after(3);fetch=fetch_fixture(b,a)
        observe(self.root,fetch,lambda:NOW+BAR+pd.Timedelta(seconds=3),no_model)
        def fail(*a):raise ConnectionError('mock secret URL must not reach journal')
        failed=observe(self.root,fail,lambda:NOW+2*BAR+pd.Timedelta(seconds=3),no_model);self.assertEqual(failed['status'],'ERROR')
        recovered=observe(self.root,fetch,lambda:NOW+3*BAR+pd.Timedelta(seconds=3),no_model)
        self.assertEqual(recovered['last_processed_M5'],ts(NOW+2*BAR))
        with Ledger(self.root/'session') as j:self.assertEqual(len(rows(j,'BAR')),3)
        self.assertNotIn('mock secret', (self.root/'session/events.csv').read_text())
    def test_active_gap_does_not_advance_past_missing_bar(self):
        b,a=bars_after(3);b=b.drop(NOW+BAR);a=a.drop(NOW+BAR)
        result=observe(self.root,fetch_fixture(b,a),lambda:NOW+3*BAR+pd.Timedelta(seconds=3),no_model)
        self.assertEqual(result['status'],'DATA_GAP');self.assertEqual(result['last_M5_open'],ts(NOW))
        b,a=bars_after(3);result=observe(self.root,fetch_fixture(b,a),lambda:NOW+3*BAR+pd.Timedelta(seconds=4),no_model)
        self.assertEqual(result['last_M5_open'],ts(NOW+2*BAR))
        with Ledger(self.root/'session') as j:self.assertEqual(len(rows(j,'BAR')),3)
    def test_seed_does_not_publish_or_analyse_old_signals(self):
        with Ledger(self.root/'session') as j:self.assertEqual(len(j.events),1);self.assertFalse(rows(j,'SIGNAL'))
    def test_no_implicit_seed_when_state_missing(self):
        with self.assertRaises(FileNotFoundError):observe(Path(self.tmp.name)/'missing',lambda *a:None,lambda:NOW,no_model)
    def test_release_change_refused(self):
        with patch.object(runner,'release_hash',return_value='changed'):
            with self.assertRaises(IntegrityError):verify(self.root)
    def test_execution_is_hard_forbidden(self):
        with SignalLedger(self.root/'session') as j:
            for kind in ['ENTRY','EXIT']:
                with self.assertRaises(IntegrityError):j.append(kind,'bad',ts(NOW),ts(NOW),{})
    def test_closed_market_no_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            close=pd.Timestamp('2030-01-11T22:00Z');b,a=history();b.index=pd.date_range(close-pd.Timedelta(hours=1),periods=12,freq='5min');a.index=b.index
            seed(tmp,b,a,close)
            def fail(*a):self.fail('No weekend request expected')
            result=observe(tmp,fail,lambda:close+BAR,no_model);self.assertEqual(result['status'],'MARKET_CLOSED')
    def test_unclosed_bar_not_processed(self):
        b,a=bars_after(2);result=observe(self.root,fetch_fixture(b,a),lambda:NOW+2*BAR-pd.Timedelta(seconds=1),no_model)
        self.assertEqual(result['last_M5_open'],ts(NOW))

class DurableBranch(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name)
        self.env=patch.dict(os.environ,{'PATH':str(GIT_FALLBACK)+os.pathsep+os.environ['PATH']} if GIT_FALLBACK.exists() else {});self.env.start()
        self.remote=self.base/'remote.git';subprocess.run(['git','init','--bare','--quiet',str(self.remote)],check=True)
        self.root=self.base/'seed';b,a=history();seed(self.root,b,a,NOW)
        GitStore(self.root,remote=str(self.remote)).upload_seed()
    def tearDown(self):self.env.stop();self.tmp.cleanup()
    def clone(self,name):return GitStore(self.base/name,remote=str(self.remote)).restore()
    def test_persist_restore_csv_prefixes_and_checkpoint(self):
        store=self.clone('one');before=(store.root/'session/events.csv').read_bytes();b,a=bars_after(3)
        observe(store.root,fetch_fixture(b,a),lambda:NOW+3*BAR+pd.Timedelta(seconds=3),no_model);store.save()
        second=self.clone('two');self.assertTrue((second.root/'session/events.csv').read_bytes().startswith(before))
        with Ledger(second.root/'session') as j:self.assertEqual(len(rows(j,'BAR')),3);self.assertEqual(rows(j,'ANALYSIS_CHECKPOINT')[-1]['bar_open_utc'],ts(NOW+2*BAR))
    def test_stale_concurrent_writer_cannot_overwrite(self):
        one,two=self.clone('one'),self.clone('two')
        for store,label in [(one,'one'),(two,'two')]:
            with Ledger(store.root/'session') as j:j.append('ERROR',label,ts(NOW),ts(NOW),{'reason':label})
        one.save()
        with self.assertRaises(StoreError):two.save()
        last=self.clone('last')
        with Ledger(last.root/'session') as j:self.assertEqual([r['reason'] for r in rows(j,'ERROR')],['one'])
    def test_existing_branch_seed_refused(self):
        new=self.base/'new';b,a=history();seed(new,b,a,NOW)
        with self.assertRaises(StoreError):GitStore(new,remote=str(self.remote)).upload_seed()
    def test_missing_branch_restore_fails_closed(self):
        remote=self.base/'empty.git';subprocess.run(['git','init','--bare','--quiet',str(remote)],check=True)
        with self.assertRaises(StoreError):GitStore(self.base/'fail',remote=str(remote)).restore()
    def test_secret_file_refused(self):
        one=self.clone('one');(one.root/'.env').write_text('not-a-real-secret')
        with self.assertRaises(StoreError):one.save()
    def test_token_not_written_in_git_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=GitStore(tmp,repository='owner/repo',token='synthetic-token')
            self.assertNotIn('synthetic-token',str(store.remote));self.assertFalse(Path(tmp,'.git').exists())

class Notifications(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'state';b,a=history();seed(self.root,b,a,NOW)
        self.env=patch.dict(os.environ,{'PAPER_WATCH_TELEGRAM_ENABLED':'true'});self.env.start();self.sent=[];self.commits=[]
    def tearDown(self):self.env.stop();self.tmp.cleanup()
    def persist(self):self.commits.append((self.root/'session/notifications/events.csv').read_text())
    def sender(self,text):self.sent.append(text);return 123
    def activate(self):return publish(self.root,self.persist,approved=True,sender=self.sender,now_fn=lambda:NOW)
    def signal(self,name='new',at=None,received=None):
        at=at or NOW+BAR;received=received or at+pd.Timedelta(seconds=3)
        data={'signal_id':name,'direction':'BUY','signal_utc':ts(at),'signal_received_utc':ts(received),'entry_due_utc':ts(at+BAR),'D1_level':1.25,'entry':1.2501,'SL':1.249,'TP':1.2523,'spread_pips':1.}
        with Ledger(self.root/'session') as j:j.append('SIGNAL',name,ts(at),ts(received),data)
        return data
    def test_disabled_never_sends(self):
        self.signal();result=publish(self.root,lambda:None,sender=self.sender);self.assertEqual(result['status'],'DISABLED');self.assertFalse(self.sent)
    def test_activation_skips_entire_old_backlog(self):
        self.signal(at=NOW-BAR);result=self.activate();self.assertEqual(result['status'],'ACTIVATED_NO_BACKLOG');self.assertFalse(self.sent)
    def test_new_signal_send_once_and_paper_label(self):
        self.activate();self.signal();when=lambda:NOW+BAR+pd.Timedelta(seconds=4)
        for _ in range(2):publish(self.root,self.persist,approved=True,sender=self.sender,now_fn=when)
        self.assertEqual(len(self.sent),1);self.assertIn('PAPER | DEMO',self.sent[0]);self.assertIn('не исполнение',self.sent[0]);self.assertIn('NOTIFY_INTENT',self.commits[-2])
    def test_old_catchup_and_late_received_not_sent(self):
        self.activate();self.signal('stale',NOW+BAR);self.signal('late',NOW+2*BAR,NOW+3*BAR)
        publish(self.root,self.persist,approved=True,sender=self.sender,now_fn=lambda:NOW+5*BAR)
        self.assertFalse(self.sent)
    def test_future_signal_not_sent(self):
        self.activate();self.signal();publish(self.root,self.persist,approved=True,sender=self.sender,now_fn=lambda:NOW+BAR-pd.Timedelta(seconds=1));self.assertFalse(self.sent)
    def test_persistence_failure_before_send_prevents_network(self):
        self.activate();self.signal()
        def fail():raise StoreError('push failed')
        with self.assertRaises(StoreError):publish(self.root,fail,approved=True,sender=self.sender,now_fn=lambda:NOW+BAR+pd.Timedelta(seconds=4))
        self.assertFalse(self.sent)
    def test_uncertain_send_no_retry(self):
        self.activate();self.signal();calls=[]
        def fail(text):calls.append(text);raise TimeoutError('uncertain')
        result=publish(self.root,self.persist,approved=True,sender=fail,now_fn=lambda:NOW+BAR+pd.Timedelta(seconds=4));self.assertEqual(result['status'],'UNCERTAIN_DELIVERY')
        publish(self.root,self.persist,approved=True,sender=self.sender,now_fn=lambda:NOW+BAR+pd.Timedelta(seconds=5));self.assertEqual(len(calls),1);self.assertFalse(self.sent)
    def test_crash_after_network_before_result_persist_no_retry(self):
        self.activate();self.signal();count=[]
        def persist():
            count.append(1)
            if len(count)==2:raise StoreError('after network')
        with self.assertRaises(StoreError):publish(self.root,persist,approved=True,sender=self.sender,now_fn=lambda:NOW+BAR+pd.Timedelta(seconds=4))
        publish(self.root,self.persist,approved=True,sender=self.sender,now_fn=lambda:NOW+BAR+pd.Timedelta(seconds=5));self.assertEqual(len(self.sent),1)
    def test_second_gate_required(self):
        with patch.dict(os.environ,{'PAPER_WATCH_TELEGRAM_ENABLED':'false'}):
            result=publish(self.root,self.persist,approved=True,sender=self.sender)
        self.assertEqual(result['status'],'DISABLED');self.assertFalse(self.sent)

class ArchivedIntegration(unittest.TestCase):
    def test_real_saved_three_candles_update_H1_no_historical_alert(self):
        repo=Path(__file__).resolve().parents[2];data=repo/'research/data';archive=repo.parent/'gbpusd-paper-watch-technical-test'
        if not (data/'GBPUSD_BID_M5_CLEAN.csv').exists() or not (archive/'last_session_ticks.csv').exists():self.skipTest('Optional local archived source data')
        b,a=read_pair(data/'GBPUSD_BID_M5_CLEAN.csv',data/'GBPUSD_ASK_M5_CLEAN.csv');nb,na=read_pair(archive/'bridge_bid.csv',archive/'bridge_ask.csv')
        b=pd.concat([b,nb]);a=pd.concat([a,na]);raw=pd.read_csv(archive/'last_session_ticks.csv',index_col=0);raw.index=pd.to_datetime(raw.index,utc=True,format='ISO8601')
        end=pd.Timestamp('2026-10-23T21:00:03Z');cut=pd.Timestamp('2026-10-23T20:40Z')
        with tempfile.TemporaryDirectory() as tmp:
            seed(tmp,b.loc[:cut],a.loc[:cut],cut+BAR)
            result=observe(tmp,lambda start,end:raw[(raw.index>=start)&(raw.index<=end)],lambda:end)
            self.assertEqual(result['status'],'OK');self.assertEqual(result['last_M5_open'],ts(nb.index[-1]))
            with Ledger(Path(tmp)/'session') as j:
                self.assertEqual(len(rows(j,'BAR')),3);self.assertFalse(rows(j,'SIGNAL'));self.assertFalse(rows(j,'ENTRY'))
            from .github_runner import SignalEngine
            from . import model
            with Ledger(Path(tmp)/'session') as j:
                e=SignalEngine(j);d,snap,h,*_=model.analyse(e.b,e.a)
                self.assertTrue(h.loc['2026-10-23T20:00Z','valid']);self.assertTrue(d.iloc[-1]['valid'])
                self.assertTrue(e.b.loc[nb.index[-3:]].equals(nb.iloc[-3:]))

class HostedFailureBoundaries(unittest.TestCase):
    def test_saved_bar_analysis_failure_recovers_on_next_invocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            b,a=history();seed(tmp,b,a,NOW);fullb,fulla=bars_after(1);when=NOW+BAR+pd.Timedelta(seconds=3)
            def fail(b,a):raise RuntimeError('injected model crash')
            result=observe(tmp,fetch_fixture(fullb,fulla),lambda:when,fail);self.assertEqual(result['status'],'ERROR')
            with Ledger(Path(tmp)/'session') as j:self.assertEqual(len(rows(j,'BAR')),1);self.assertFalse(rows(j,'ANALYSIS_CHECKPOINT'))
            observe(tmp,fetch_fixture(fullb,fulla),lambda:when+pd.Timedelta(seconds=1),one_signal)
            with Ledger(Path(tmp)/'session') as j:
                self.assertEqual(len(rows(j,'BAR')),1);self.assertEqual(len(rows(j,'SIGNAL')),1);self.assertEqual(len(rows(j,'ANALYSIS_CHECKPOINT')),1);self.assertFalse(rows(j,'ENTRY'))
    def test_revision_reports_halt_not_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            b,a=history();seed(tmp,b,a,NOW);b.loc[b.index[-1],'high']+=.0001;a=b+.0001
            result=observe(tmp,fetch_fixture(b,a),lambda:NOW+BAR,no_model);self.assertEqual(result['status'],'HALTED')
            with self.assertRaises(IntegrityError):observe(tmp,fetch_fixture(b,a),lambda:NOW+BAR,no_model)

class CorruptedState(unittest.TestCase):
    def test_state_with_incomplete_authoritative_tail_not_uploaded(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'PATH':str(GIT_FALLBACK)+os.pathsep+os.environ['PATH']} if GIT_FALLBACK.exists() else {}):
            root=Path(tmp)/'state';b,a=history();seed(root,b,a,NOW)
            with (root/'session/events.csv').open('a') as f:f.write('partial')
            remote=Path(tmp)/'remote.git';subprocess.run(['git','init','--bare','--quiet',str(remote)],check=True)
            with self.assertRaises(IntegrityError):GitStore(root,remote=str(remote)).upload_seed()

class StrictUploadAllowlist(unittest.TestCase):
    def test_unknown_csv_cannot_leak_to_state_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            b,a=history();seed(tmp,b,a,NOW);Path(tmp,'session/tokens.csv').write_text('not-a-real-secret')
            store=GitStore(tmp,repository='owner/repo',token='synthetic-token')
            with self.assertRaises(StoreError):store.validate_files()

class NotificationAmbiguityReporting(unittest.TestCase):
    setUp=Notifications.setUp
    tearDown=Notifications.tearDown
    persist=Notifications.persist
    sender=Notifications.sender
    activate=Notifications.activate
    signal=Notifications.signal
    def test_unresolved_intent_is_reported_after_restart(self):
        self.activate();self.signal()
        def fail():raise StoreError('intent save failed')
        with self.assertRaises(StoreError):publish(self.root,fail,approved=True,sender=self.sender,now_fn=lambda:NOW+BAR+pd.Timedelta(seconds=4))
        result=publish(self.root,self.persist,approved=True,sender=self.sender,now_fn=lambda:NOW+BAR+pd.Timedelta(seconds=5))
        self.assertEqual(result['status'],'UNCERTAIN_DELIVERY');self.assertEqual(result['unknown'],1);self.assertFalse(self.sent)
