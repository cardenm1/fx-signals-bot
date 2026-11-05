"""End-to-end candle fixtures: the BOS detector and state machine are not mocked."""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import pandas as pd
import liquidity_alerts as bot


class OneShotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state.json'
        self.state_patch = patch.object(bot, 'STATE_FILE', self.path)
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.state = bot.load_state()
        self.api = SimpleNamespace(send_message=AsyncMock())

    def scenario(self, *, lower=False, reversal=False, returned=False, periods=40):
        end = pd.Timestamp.now(tz='UTC').floor('min') - pd.Timedelta(minutes=1)
        idx = pd.date_range(end=end, periods=periods, freq='min')
        f = pd.DataFrame(dict(open=1., high=1.1, low=.9, close=1.), index=idx)
        f.loc[idx[-12], 'high'] = 1.3
        f.loc[idx[-9], 'low'] = .7
        f.loc[idx[-5], ['high', 'close']] = [1.25, 1.15 if returned else 1.22]
        f.loc[idx[-4], ['open', 'high', 'low', 'close']] = [1.22, 1.27, 1.21, 1.25]
        if reversal:
            f.loc[idx[-3], ['open', 'high', 'low', 'close']] = [1., 1.1, .6, .65]
        else:
            f.loc[idx[-3], ['open', 'high', 'low', 'close']] = [1.25, 1.4, 1.21, 1.35]
        if lower:
            f = pd.DataFrame(dict(open=2-f.open, high=2-f.low,
                                  low=2-f.high, close=2-f.close), index=idx)
        level = .8 if lower else 1.2
        self.key = f'GBPUSD:{level:.5f}'
        self.state['levels'][self.key] = dict(pair='GBPUSD', level=level,
            created=idx[-6].isoformat(), status='watching')
        self.state['last_bar']['GBPUSD'] = idx[-6].isoformat()
        return f

    def run_frame(self, f):
        with patch.object(bot, 'fetch_m1', return_value=f):
            asyncio.run(bot.analyze_pair(self.api, self.state, 'GBPUSD'))

    def test_four_directions_with_real_bos(self):
        for lower in (False, True):
            for reversal in (False, True):
                with self.subTest(lower=lower, reversal=reversal):
                    self.state = dict(
                        offset=0, levels={}, setups={}, last_bar={}, sent=[])
                    self.api.send_message.reset_mock()
                    f = self.scenario(lower=lower, reversal=reversal)
                    self.run_frame(f)
                    self.api.send_message.assert_awaited_once()
                    text = self.api.send_message.call_args.kwargs['text']
                    direction = 'SELL' if lower != reversal else 'BUY'
                    self.assertIn(f'Сигнал: {direction}', text)
                    self.assertIn('РАЗВОРОТ' if reversal else 'ПРОДОЛЖЕНИЕ', text)
                    self.assertIn('снятие с возвратом за уровень' if reversal
                                  else 'пробой без возврата', text)
                    self.assertEqual(self.state['levels'][self.key]['status'], 'completed')

    def test_return_then_continuation_retains_return(self):
        f = self.scenario(returned=True)
        self.run_frame(f)
        self.assertIn('снятие с возвратом за уровень',
                      self.api.send_message.call_args.kwargs['text'])
        self.assertIn('ПРОДОЛЖЕНИЕ', self.api.send_message.call_args.kwargs['text'])

    def test_restart_while_waiting_retains_return(self):
        f = self.scenario(returned=True)
        self.run_frame(f.iloc[:-3])
        self.api.send_message.assert_not_awaited()
        bot.save_state(self.state)
        self.state = bot.load_state()
        self.assertTrue(self.state['setups'][self.key]['returned'])
        self.run_frame(f)
        self.api.send_message.assert_awaited_once()

    def test_completed_level_never_rearms_after_restart_and_timeout(self):
        f = self.scenario()
        self.run_frame(f)
        self.state = bot.load_state()
        self.state['last_bar']['GBPUSD'] = f.index[-6].isoformat()
        self.run_frame(f)  # Explicitly replay the sweep and BOS.
        later = f.copy()
        later.index += pd.Timedelta(minutes=180)
        with patch.object(bot.pd.Timestamp, 'now', return_value=later.index[-1] + pd.Timedelta(minutes=1)):
            self.run_frame(later)
        self.api.send_message.assert_awaited_once()

    def test_missing_minute_blocks_level_durably(self):
        f = self.scenario()
        self.run_frame(f.drop(f.index[-4]))
        self.api.send_message.assert_not_awaited()
        self.assertEqual(self.state['levels'][self.key]['status'], 'incomplete_history')
        bot.save_state(self.state)
        self.state = bot.load_state()
        self.run_frame(f)
        self.api.send_message.assert_not_awaited()

    def test_history_window_does_not_cover_checkpoint(self):
        f = self.scenario()
        self.state['last_bar']['GBPUSD'] = (f.index[0] - pd.Timedelta(minutes=10)).isoformat()
        self.state['levels'][self.key]['created'] = (f.index[0] - pd.Timedelta(minutes=20)).isoformat()
        self.run_frame(f)
        self.api.send_message.assert_not_awaited()
        self.assertEqual(self.state['levels'][self.key]['status'], 'incomplete_history')

    def test_wick_only_bos_does_not_signal(self):
        f = self.scenario()
        f.loc[f.index[-3], 'close'] = 1.29
        self.run_frame(f)
        self.api.send_message.assert_not_awaited()

    def test_touch_only_does_not_arm(self):
        f = self.scenario()
        f.loc[f.index[-5]:, ['open', 'high', 'low', 'close']] = [1., 1.2, .9, 1.]
        self.run_frame(f)
        self.assertFalse(self.state['setups'])
        self.api.send_message.assert_not_awaited()

    def test_bad_or_unclosed_candles_are_rejected(self):
        for kind in ('duplicate', 'unordered', 'nan', 'ohlc', 'unclosed'):
            with self.subTest(kind=kind):
                f = self.scenario()
                if kind == 'duplicate':
                    f = pd.concat([f, f.iloc[-1:]])
                elif kind == 'unordered':
                    f = f.iloc[::-1]
                elif kind == 'nan':
                    f.iloc[-1, 0] = float('nan')
                elif kind == 'ohlc':
                    f.iloc[-1, f.columns.get_loc('high')] = .5
                else:
                    f.index += pd.Timedelta(minutes=1)
                with self.assertRaises(ValueError):
                    self.run_frame(f)
                self.api.send_message.assert_not_awaited()

    def test_regular_two_minute_bars_are_not_m1_structure(self):
        f = self.scenario()
        f.index = pd.date_range(end=f.index[-1], periods=len(f), freq='2min')
        self.assertIsNone(bot.bos_at(f, len(f)-3))

    def test_legacy_publication_and_pending_migration(self):
        f = self.scenario()
        item = self.state['levels'][self.key]
        del item['status']
        self.state['setups'][self.key] = dict(time=f.index[-5].isoformat(), direction='UP', sent_tf=['M1'])
        bot.save_state(self.state)
        restored = bot.load_state()
        self.assertEqual(restored['levels'][self.key]['status'], 'completed')
        self.state['setups'][self.key]['sent_tf'] = []
        bot.save_state(self.state)
        restored = bot.load_state()
        self.assertEqual(restored['levels'][self.key]['status'], 'incomplete_history')

    def test_fresh_registration_does_not_need_old_pair_checkpoint(self):
        f = self.scenario()
        self.state['last_bar']['GBPUSD'] = (f.index[0] - pd.Timedelta(days=3)).isoformat()
        self.run_frame(f)
        self.api.send_message.assert_awaited_once()

    def test_legacy_level_without_journal_is_not_rearmed(self):
        self.scenario()
        del self.state['levels'][self.key]['status']
        bot.save_state(self.state)
        self.assertEqual(bot.load_state()['levels'][self.key]['status'], 'incomplete_history')

    def test_backwards_data_does_not_regress_checkpoint(self):
        f = self.scenario()
        checkpoint = (f.index[-1] + pd.Timedelta(minutes=1)).isoformat()
        self.state['last_bar']['GBPUSD'] = checkpoint
        with self.assertRaises(ValueError):
            self.run_frame(f)
        self.assertEqual(self.state['last_bar']['GBPUSD'], checkpoint)
        self.api.send_message.assert_not_awaited()

    def test_expiry_is_terminal(self):
        f = self.scenario(periods=180)
        self.state['levels'][self.key]['status'] = 'waiting_bos'
        self.state['setups'][self.key] = dict(time=f.index[0].isoformat(),
                                            direction='UP', returned=False)
        self.run_frame(f)
        self.assertEqual(self.state['levels'][self.key]['status'], 'expired')
        self.assertFalse(self.state['setups'])
        self.api.send_message.assert_not_awaited()

if __name__ == '__main__':
    unittest.main()
