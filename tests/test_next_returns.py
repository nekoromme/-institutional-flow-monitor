"""新条件で日付・分割・先読みを取り違えると利益が変わる部分を検査する。"""
from copy import deepcopy
from datetime import date, datetime, timedelta
import unittest

from flow_probe.alpaca import NY
from flow_probe.next_returns import add_price_direction
from flow_probe.execution_followup import quote_reference, dividend_shapes
from flow_probe.returns_core import schedule
from flow_probe.return_diagnostics import validate_price_calendar


def market(start='2026-04-20',symbol='X'):
    first=date.fromisoformat(start)
    days=[(first+timedelta(days=i)).isoformat() for i in range(80) if (first+timedelta(days=i)).weekday()<5]
    book={d:{'valid':True,'raw':{'o':100,'h':110,'l':90,'c':100,'v':1000}} for d in days}
    return days,{symbol:book}


class NextReturnTests(unittest.TestCase):
    def test_explicit_period_evaluates_may_without_rewriting_old_default(self):
        days,_=market();row={'symbol':'X','date':'2026-05-01','abnormal':True}
        self.assertEqual(schedule([row],days,'abnormal',10)[0],[])
        rows,_=schedule([row],days,'abnormal',10,start='2026-05-01',end='2026-06-30')
        self.assertEqual(rows[0]['entry_date'],'2026-05-04')
        self.assertEqual(rows[0]['exit_date'],'2026-05-15')

    def test_skipping_first_signal_allows_different_later_entry(self):
        days,books=market();books['X']['2026-05-01']['raw']['c']=90
        books['X']['2026-05-06']['raw']['c']=105
        signals=[{'symbol':'X','date':d,'repeated':True} for d in ('2026-05-01','2026-05-06')]
        rows=add_price_direction(signals,books,days)
        b,_=schedule(rows,days,'repeated',10,start='2026-05-01',end='2026-06-30')
        c,_=schedule(rows,days,'repeated_up',10,start='2026-05-01',end='2026-06-30')
        self.assertEqual(b[0]['signal_date'],'2026-05-01')
        self.assertEqual(c[0]['signal_date'],'2026-05-06')

    def test_missing_inside_direction_window_is_unknown_not_zero(self):
        days,books=market();books['X']['2026-04-29']['valid']=False
        rows=add_price_direction([{'symbol':'X','date':'2026-05-01','repeated':True}],books,days)
        self.assertIsNone(rows[0]['repeated_up'])

    def test_future_price_does_not_change_known_direction(self):
        days,books=market();signals=[{'symbol':'X','date':'2026-05-01','repeated':True}]
        before=add_price_direction(signals,books,days)
        books['X']['2026-05-04']['raw']['c']=10000
        self.assertEqual(before,add_price_direction(signals,books,days))

    def test_documented_split_does_not_create_price_up_signal(self):
        days,books=market('2026-05-20','SMSI')
        for d,b in books['SMSI'].items():
            if d>='2026-06-05':b['raw']['c']=500
        rows=add_price_direction([{'symbol':'SMSI','date':'2026-06-08','repeated':True}],books,days)
        self.assertEqual(rows[0]['known_five_day_price_change'],0)
        self.assertFalse(rows[0]['repeated_up'])

    def test_unknown_repetition_is_not_made_known_by_rising_price(self):
        days,books=market();books['X']['2026-05-01']['raw']['c']=105
        rows=add_price_direction([{'symbol':'X','date':'2026-05-01','repeated':None}],books,days)
        self.assertIsNone(rows[0]['repeated_up'])

    def test_quote_does_not_use_future_stale_or_crossed_market(self):
        moment=datetime(2026,1,12,9,31,tzinfo=NY)
        row={'t':'2026-01-12T14:30:59Z','bp':99,'ap':101,'bs':1,'as':1}
        for bad in [{**row,'t':'2026-01-12T14:31:01Z'},{**row,'t':'2026-01-12T14:29:59Z'},
                    {**row,'bp':102}]:
            self.assertEqual(quote_reference([bad],moment,100,'entry',complete=True)['status'],'unknown')
        r=quote_reference([row],moment,100,'entry',complete=True)
        self.assertAlmostEqual(r['full_spread_fraction_of_mid'],0.02)
        self.assertAlmostEqual(r['quote_side_relative_to_daily_reference'],0.01)
        self.assertEqual(quote_reference([row],moment,100,'entry',complete=False)['status'],'unknown')

    def test_additive_dividend_shape_is_reported_without_changing_price_return(self):
        a={'t':'2026-01-12T05:00:00Z','o':100,'h':110,'l':90,'c':105,'v':1000}
        b={**a,**{k:a[k]-5 for k in ('o','h','l','c')}}
        result=dividend_shapes({'split':{'X':[a]},'split,dividend':{'X':[b]}})
        self.assertEqual(result['rows'][0]['uniform_offset_only'],1)
        self.assertFalse(result['used_to_change_primary_returns'])

    def test_new_calendar_can_include_july_followup(self):
        sessions=[{'date':'2026-07-31','open':'09:30','close':'16:00'}]
        self.assertEqual(validate_price_calendar(sessions,start='2025-12-15',end='2026-07-31'),['2026-07-31'])


if __name__=='__main__':unittest.main()
