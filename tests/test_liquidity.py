import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch, Mock
import pandas as pd
import requests
import yaml
import liquidity_alerts as bot
import liquidity_feed as feed
import liquidity_state as store


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / store.FILENAME
        self.patches = [patch.object(bot, 'STATE_FILE', self.path),
                        patch.object(bot, 'CHAT_ID', '42')]
        for p in self.patches: p.start()
        self.state = bot.load_state()
        self.api = NS(send_message=AsyncMock(), get_updates=AsyncMock())

    def tearDown(self):
        for p in self.patches: p.stop()
        self.tmp.cleanup()

    def command(self, *texts, chat=42):
        self.api.get_updates.return_value = [NS(update_id=i + self.state['offset'],
            effective_message=NS(text=t), effective_chat=NS(id=chat))
            for i, t in enumerate(texts)]
        asyncio.run(bot.handle_commands(self.api, self.state))

    def frame(self):
        end = pd.Timestamp.now(tz='UTC').floor('5min') - pd.Timedelta(minutes=1)
        idx = pd.date_range(end=end, periods=50, freq='min')
        return pd.DataFrame({'open':1., 'high':1.1, 'low':.9, 'close':1.}, index=idx)

    def level(self, frame):
        self.state['levels']['GBPUSD:1.20000'] = dict(pair='GBPUSD', level=1.2,
             created=(frame.index[0] - pd.Timedelta(minutes=1)).isoformat())
        self.state['last_bar']['GBPUSD'] = frame.index[-4].isoformat()

    def test_commands_and_reload(self):
        self.command('/add GBP/USD 1.234567', '/list', '/status')
        self.assertIn('GBPUSD:1.23457', bot.load_state()['levels'])
        self.command('/add GBPUSD 1.23457')
        self.assertEqual(len(self.state['levels']), 1)
        self.command('/remove GBPUSD 1.23457', '/list')
        self.assertFalse(bot.load_state()['levels'])
        self.assertEqual(self.state['offset'], 6)

    def test_invalid_commands(self):
        self.command('/add', '/add XX 1', '/add GBPUSD nan', '/add GBPUSD inf',
                     '/add GBPUSD 0', '/remove XX nan', '/remove GBPUSD inf')
        self.assertFalse(self.state['levels'])
        self.assertEqual(self.api.send_message.await_count, 7)

    def test_foreign_chat(self):
        self.command('/add GBPUSD 1.2', chat=9)
        self.assertFalse(self.state['levels'])
        self.api.send_message.assert_not_awaited()

    def test_corrupt_state_fails_closed(self):
        for text in ('{', '[]', '{"levels": []}'):
            self.path.write_text(text)
            with self.assertRaises((ValueError, RuntimeError)): bot.load_state()

    def test_command_saved_before_reply_failure(self):
        self.api.send_message.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError): self.command('/add USDJPY 150.1234')
        self.assertIn('USDJPY:150.123', bot.load_state()['levels'])
        self.assertEqual(bot.load_state()['offset'], 1)

    def test_m5_requires_five_closed_minutes(self):
        f = self.frame()
        complete = bot.make_m5(f)
        self.assertEqual(len(complete), 10)
        missing = bot.make_m5(f.drop(f.index[-3]))
        self.assertEqual(len(missing), 9)

    def test_real_pivot_bos_no_lookahead(self):
        f = self.frame().iloc[:10].copy()
        f['high'] = [1, 2, 3, 2, 1, 2, 2, 4, 2, 2]
        f['close'] = [.5, 1, 2, 1, .5, 1, 2, 3.5, 1, 1]
        self.assertEqual(bot.bos_at(f, 7), 'BUY')
        f.iloc[8:, f.columns.get_loc('high')] = 100
        self.assertEqual(bot.bos_at(f, 7), 'BUY')
        f['low'] = -f['high']; f['close'] = -f['close']
        self.assertEqual(bot.bos_at(f, 7), 'SELL')

    def test_signal_and_restart_dedup(self):
        f = self.frame(); self.level(f)
        f.loc[f.index[-8], 'high'] = 1.3
        self.state['last_bar']['GBPUSD'] = f.index[-9].isoformat()
        with patch.object(bot, 'fetch_m1', return_value=f), patch.object(bot, 'bos_at', return_value='BUY'):
            asyncio.run(bot.analyze_pair(self.api, self.state, 'GBPUSD'))
            self.assertEqual(self.api.send_message.await_count, 2)  # M1 + closed M5
            self.state = bot.load_state()
            asyncio.run(bot.analyze_pair(self.api, self.state, 'GBPUSD'))
            self.assertEqual(self.api.send_message.await_count, 2)

    def test_no_bos_on_sweep_bar(self):
        f = self.frame(); self.level(f)
        f.loc[f.index[-1], 'high'] = 1.3
        with patch.object(bot, 'fetch_m1', return_value=f), patch.object(bot, 'bos_at', return_value='BUY'):
            asyncio.run(bot.analyze_pair(self.api, self.state, 'GBPUSD'))
        self.api.send_message.assert_not_awaited()

    def test_no_sweep_no_signal(self):
        f = self.frame(); self.level(f)
        with patch.object(bot, 'fetch_m1', return_value=f), patch.object(bot, 'bos_at', return_value='BUY'):
            asyncio.run(bot.analyze_pair(self.api, self.state, 'GBPUSD'))
        self.api.send_message.assert_not_awaited()

    def test_ambiguous_delivery_never_retried(self):
        f = self.frame(); self.level(f); f.loc[f.index[-3], 'high'] = 1.3
        self.api.send_message.side_effect = TimeoutError()
        with patch.object(bot, 'fetch_m1', return_value=f), patch.object(bot, 'bos_at', return_value='BUY'):
            with self.assertRaises(TimeoutError): asyncio.run(bot.analyze_pair(self.api, self.state, 'GBPUSD'))
            restored = bot.load_state()
            self.assertEqual(len(restored['sent']), 1)
            self.assertIn('M1', restored['setups']['GBPUSD:1.20000']['sent_tf'])

    def test_remote_checkpoint_failure_prevents_send(self):
        f = self.frame(); self.level(f); f.loc[f.index[-3], 'high'] = 1.3
        with patch.object(bot, 'fetch_m1', return_value=f), patch.object(bot, 'bos_at', return_value='BUY'), patch.object(bot, 'save_state', side_effect=RuntimeError('push')):
            with self.assertRaises(RuntimeError): asyncio.run(bot.analyze_pair(self.api, self.state, 'GBPUSD'))
        self.api.send_message.assert_not_awaited()

    def test_expired_setup(self):
        f = self.frame(); self.level(f)
        self.state['setups']['GBPUSD:1.20000'] = dict(time=(f.index[-1]-pd.Timedelta(hours=3)).isoformat(), direction='UP', sent_tf=[])
        with patch.object(bot, 'fetch_m1', return_value=f): asyncio.run(bot.analyze_pair(self.api, self.state, 'GBPUSD'))
        self.assertFalse(self.state['setups'])

    def test_stale_quotes_fail(self):
        f = self.frame(); f.index -= pd.Timedelta(days=1)
        with patch.object(bot, 'fetch_m1', return_value=f):
            with self.assertRaises(ValueError): asyncio.run(bot.analyze_pair(self.api, self.state, 'GBPUSD'))

    def test_first_run_skips_history(self):
        f = self.frame(); self.level(f); self.state['last_bar'] = {}
        with patch.object(bot, 'fetch_m1', return_value=f): asyncio.run(bot.analyze_pair(self.api, self.state, 'GBPUSD'))
        self.api.send_message.assert_not_awaited()

    def test_feed_tick_aggregation_and_unclosed_bar(self):
        now = pd.Timestamp('2026-10-28T12:05:30Z')
        times = pd.to_datetime(['2026-10-28T12:03:00Z','2026-10-28T12:03:30Z','2026-10-28T12:04:00Z','2026-10-28T12:05:00Z'])
        rows = [[int(t.timestamp()*1000), v, v+.01, 1, 1] for t,v in zip(times,[1,2,3,4])]
        session=Mock(); session.get.return_value=NS(text='liquidity('+json.dumps(rows)+');', raise_for_status=lambda:None)
        frame=feed.fetch_m1('NZDCHF', now=now, session=session)
        self.assertEqual(frame.iloc[0].to_dict(), dict(open=1.,high=2.,low=1.,close=2.))
        self.assertEqual(len(frame),2)
        self.assertEqual(session.get.call_args.kwargs['params']['instrument'],'NZD/CHF')
        self.assertIn('timeout',session.get.call_args.kwargs)

    def test_feed_http_error(self):
        session=Mock(); session.get.side_effect=requests.Timeout()
        with self.assertRaises(requests.Timeout): feed.fetch_m1('EURUSD',session=session)

    def test_feed_bad_response(self):
        session=Mock(); session.get.return_value=NS(text='blocked',raise_for_status=lambda:None)
        with self.assertRaises(ValueError): feed.fetch_m1('EURUSD',session=session)

    def test_state_git_roundtrip(self):
        remote=Path(self.tmp.name)/'remote'; remote.mkdir()
        store.git(remote,'init','--bare','--quiet')
        first=Path(self.tmp.name)/'first'
        store.restore(first,True,str(remote))
        state=json.loads((first/store.FILENAME).read_text()); state['offset']=123
        (first/store.FILENAME).write_text(json.dumps(state)); store.persist(first)
        second=Path(self.tmp.name)/'second'; store.restore(second,False,str(remote))
        self.assertEqual(json.loads((second/store.FILENAME).read_text())['offset'],123)
        # A stale writer must not overwrite a newer checkpoint.
        state['offset']=124; (first/store.FILENAME).write_text(json.dumps(state)); store.persist(first)
        (second/store.FILENAME).write_text(json.dumps(dict(state,offset=999)))
        with self.assertRaises(RuntimeError): store.persist(second)

    def test_missing_state_requires_explicit_init(self):
        remote=Path(self.tmp.name)/'remote'; remote.mkdir(); store.git(remote,'init','--bare','--quiet')
        with self.assertRaises(RuntimeError): store.restore(Path(self.tmp.name)/'new',False,str(remote))

    def test_bos_cannot_bridge_missing_bars(self):
        f = self.frame().iloc[:10].copy()
        f['high'] = [1, 2, 3, 2, 1, 2, 2, 4, 2, 2]
        f['close'] = [.5, 1, 2, 1, .5, 1, 2, 3.5, 1, 1]
        f.index = pd.DatetimeIndex(list(f.index[:6]) + list(f.index[6:] + pd.Timedelta(minutes=1)))
        self.assertIsNone(bot.bos_at(f, 7))

    def test_actual_sweep_then_confirmed_bos(self):
        f = self.frame()
        f.iloc[-10:, f.columns.get_loc('high')] = [1.1,1.2,1.3,1.2,1.1,1.2,1.31,1.4,1.4,1.4]
        f.iloc[-10:, f.columns.get_loc('close')] = [1.,1.,1.1,1.,1.,1.,1.1,1.35,1.35,1.35]
        self.level(f)
        self.state['levels'] = {'GBPUSD:1.30000':dict(pair='GBPUSD',level=1.3,created=f.index[0].isoformat())}
        self.state['last_bar']['GBPUSD'] = f.index[-5].isoformat()
        with patch.object(bot, 'fetch_m1', return_value=f):
            asyncio.run(bot.analyze_pair(self.api,self.state,'GBPUSD'))
        self.assertEqual(self.api.send_message.await_count,1)
        self.assertIn('BOS: M1',self.api.send_message.call_args.kwargs['text'])
        self.assertIn('Сигнал: BUY',self.api.send_message.call_args.kwargs['text'])

    def test_both_sides_swept_is_ambiguous(self):
        f=self.frame(); self.level(f)
        f.loc[f.index[-1], ['high','low']]=[1.3,.8]
        f.loc[f.index[-2], ['high','low']]=[1.1,1.25]
        with patch.object(bot,'fetch_m1',return_value=f):
            asyncio.run(bot.analyze_pair(self.api,self.state,'GBPUSD'))
        self.assertFalse(self.state['setups'])

    def test_workflow_yaml(self):
        workflow=yaml.safe_load(Path('.github/workflows/liquidity-bos-demo.yml').read_text())
        self.assertIn('workflow_dispatch',workflow['on'])
        self.assertFalse(workflow['concurrency']['cancel-in-progress'])
        self.assertEqual(len(feed.PAIRS),11)

if __name__ == '__main__': unittest.main()
