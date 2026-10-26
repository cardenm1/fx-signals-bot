import unittest
from unittest.mock import patch
import pandas as pd
from abc_author.engine import Pivot,Config,signals,aggregate,pivots,four_points,engulf
from abc_author.data import load
from abc_author.backtest import execute


def frame(n=240):
    ix=pd.date_range('2026-01-05',periods=n,freq='5min',tz='UTC')
    f=pd.DataFrame(index=ix)
    for side,spread in [('bid',0),('ask',.0002)]:
        for k,v in [('open',1.1),('high',1.1002),('low',1.0998),('close',1.1)]:f[side+'_'+k]=v+spread
    f['segment']=1
    return f


def put(f,start,end,o,h,l,c):
    for side,spread in [('bid',0),('ask',.0002)]:
        for k,v in zip(('open','high','low','close'),(o,h,l,c)):
            f.loc[start:end,side+'_'+k]=v+spread


def fixture(mirror=False):
    f=frame();date='2026-01-05 '
    put(f,date+'04:00',date+'04:55',1.1022,1.104,1.102,1.103)
    put(f,date+'08:00',date+'08:55',1.098,1.0982,1.096,1.097)
    put(f,date+'10:00',date+'10:00',1.102,1.103,1.101,1.1025)
    put(f,date+'11:00',date+'11:15',1.1001,1.1002,1.0995,1.1000)
    put(f,date+'11:20',date+'11:20',1.1000,1.1012,1.0999,1.1010)
    put(f,date+'11:25',date+'11:25',1.1011,1.1012,1.0997,1.0998)
    def p(time,known,kind,price):return Pivot(pd.Timestamp(date+time,tz='UTC'),pd.Timestamp(date+known,tz='UTC'),kind,price,1)
    hp=[p('01:00','04:00','L',1.099),p('04:00','07:00','H',1.104)]
    lp=[p('10:05','10:20','H',1.1035),p('10:25','10:40','L',1.1005),p('10:45','11:00','H',1.1025),p('11:05','11:20','L',1.0995)]
    if mirror:
        for side in ('bid','ask'):
            old=f[[side+'_'+k for k in ('open','high','low','close')]].copy()
            for k,src in [('open','open'),('high','low'),('low','high'),('close','close')]:
                f[side+'_'+k]=2.2-old[side+'_'+src]+(.0004 if side=='ask' else 0)
        hp=[Pivot(x.time,x.known,'H' if x.kind=='L' else 'L',2.2-x.price,1) for x in hp]
        lp=[Pivot(x.time,x.known,'H' if x.kind=='L' else 'L',2.2-x.price,1) for x in lp]
    return f,hp,lp

class EngineTests(unittest.TestCase):
    def compute(self,f,hp,lp):
        with patch('abc_author.engine.pivots',side_effect=lambda b,m,r:hp if m==60 else lp):return signals(f,with_events=True)

    def test_full_sequence_and_mirror(self):
        for mirror in (False,True):
            f,hp,lp=fixture(mirror);s,e=self.compute(f,hp,lp)
            self.assertEqual(len(s),1)
            self.assertEqual(s[0]['direction'],'long' if mirror else 'short')
            self.assertEqual([x['event'] for x in e][:4],['impulse_confirmed','block_touch','four_point_break','engulf_entry_signal'])
            self.assertEqual(pd.Timestamp(s[0]['signal_time']),f.index[138])
            self.assertLessEqual(pd.Timestamp(s[0]['pattern_known']),pd.Timestamp(s[0]['engulf_previous']))
            self.assertAlmostEqual(s[0]['target5'],1.10401 if mirror else 1.09599)

    def test_no_engulf_no_signal(self):
        f,hp,lp=fixture();put(f,'2026-01-05 11:25','2026-01-05 11:25',1.1008,1.101,1.0997,1.1002)
        s,_=self.compute(f,hp,lp);self.assertEqual(s,[])

    def test_no_fourth_point_no_signal(self):
        f,hp,lp=fixture();s,_=self.compute(f,hp,lp[:-1]);self.assertEqual(s,[])

    def test_no_closed_htf_break_no_context(self):
        f,hp,lp=fixture();put(f,'2026-01-05 08:00','2026-01-05 08:55',1.1,1.1001,1.096,1.1)
        s,e=self.compute(f,hp,lp);self.assertEqual(s,[]);self.assertFalse(any(x['event']=='impulse_confirmed' for x in e))

    def test_no_touch_no_signal(self):
        f,hp,lp=fixture();put(f,'2026-01-05 10:00','2026-01-05 10:00',1.1,1.1001,1.0999,1.1)
        s,e=self.compute(f,hp,lp);self.assertEqual(s,[])

    def test_target_freezes_before_touch_and_future_invariance(self):
        f,hp,lp=fixture();s,_=self.compute(f,hp,lp)
        put(f,'2026-01-05 10:00','2026-01-05 10:00',1.102,1.103,1.09,1.1025)
        changed,_=self.compute(f,hp,lp)
        self.assertEqual(changed[0]['target5'],s[0]['target5'])
        cut=f.index.get_loc(pd.Timestamp(s[0]['signal_time']))
        prefix,_=self.compute(f.iloc[:cut],hp,lp)
        self.assertEqual(prefix,changed)
        put(f,'2026-01-05 13:00','2026-01-05 13:55',1.09,1.2,1.0,1.09)
        later,_=self.compute(f,hp,lp);self.assertEqual(later,changed)

    def test_htf_aggregation_and_pivot_delay(self):
        f=frame(48)
        self.assertEqual(len(aggregate(f,240)),1)
        self.assertEqual(len(aggregate(f.drop(f.index[2]),240)),0)
        self.assertEqual(len(aggregate(f,15)),16)
        b=aggregate(f.iloc[:5],5);b.loc[b.index[2],'high']=1.102
        self.assertEqual(pivots(b.iloc[:4],5),[])
        self.assertEqual(pivots(b,5)[0].known,b.index[4]+pd.Timedelta(minutes=5))

    def test_wick_engulf_alone_rejected(self):
        class Bar:pass
        a=Bar();a.open=1.1;a.close=1.101
        b=Bar();b.open=1.1009;b.close=1.1001
        self.assertFalse(engulf(a,b,-1))
        b.open=1.1011;b.close=1.0999
        self.assertTrue(engulf(a,b,-1))

class ExecutionTests(unittest.TestCase):
    def test_two_targets_costs_and_stop_first(self):
        f=frame(3);f.loc[:,'bid_high']=1.104;f.loc[:,'bid_low']=1.097
        s=dict(signal_time=str(f.index[0]),direction='long',stop=1.099,target5=1.103)
        for mode in ('2R','point5'):
            t=execute(f,s,target_mode=mode,fee_pips=.5)
            self.assertEqual(t['reason'],'stop');self.assertLess(t['r_multiple'],-1)
            self.assertAlmostEqual(t['entry'],1.10022)
        self.assertAlmostEqual(execute(f,s,target_mode='point5')['target'],1.103)

    def test_invalid_point5_and_missing_entry(self):
        f=frame(3);s=dict(signal_time=str(f.index[0]),direction='long',stop=1.099,target5=1.09)
        self.assertEqual(execute(f,s,target_mode='point5')['status'],'invalid_target')
        s['signal_time']=str(f.index[-1]+pd.Timedelta(minutes=5))
        self.assertEqual(execute(f,s)['status'],'no_next_bar')
