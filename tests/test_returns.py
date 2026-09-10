"""通知後の評価で結論を逆転させ得る、時刻・費用・不明・重複を検査する。"""
from datetime import date, timedelta
import unittest

from flow_probe.returns_core import after_cost, schedule, value_attempt, evaluate, summarize, wait_pairs
from flow_probe.return_diagnostics import prepare_books


def fixture():
    start=date(2025,12,15)
    days=[(start+timedelta(days=i)).isoformat() for i in range(137)
          if (start+timedelta(days=i)).weekday()<5]
    book={d:{'valid':True,'raw':{'o':100,'h':105,'l':95,'c':102,'v':1000},
             'split':{'o':100,'h':105,'l':95,'c':102,'v':1000},
             'dividend':{'o':100,'h':106,'l':95,'c':103,'v':1000}} for d in days}
    return days,{'X':book,'SPY':book}


def signal(day, repeated=False):
    return {'symbol':'X','date':day,'abnormal':True,'repeated':repeated}


class ReturnTests(unittest.TestCase):
    def test_round_trip_cost_includes_purchase_capital(self):
        self.assertAlmostEqual(after_cost(1,0.0025),0.9975/1.0025-1)
        self.assertAlmostEqual(after_cost(1.1,0.01),1.1*0.99/1.01-1)
        self.assertLess(after_cost(1.004,0.0025),0)
        with self.assertRaises(ValueError):after_cost(0,0.0025)

    def test_buy_after_signal_and_exit_on_tenth_holding_day(self):
        days,_=fixture();d='2026-01-02';i=days.index(d)
        rows,_=schedule([signal(d)],days,'abnormal',10)
        self.assertEqual(rows[0]['entry_date'],days[i+1])
        self.assertEqual(rows[0]['exit_date'],days[i+10])
        self.assertGreater(rows[0]['assumed_information_available_after'][:10],d)

    def test_signal_on_exit_day_does_not_start_duplicate_position(self):
        days,_=fixture();i=days.index('2026-01-02')
        rows,counts=schedule([signal(days[j]) for j in (i,i+1,i+10,i+11)],days,'abnormal',10)
        self.assertEqual(len(rows),2)
        self.assertEqual(counts['signal_while_position_reserved'],2)
        self.assertEqual(rows[1]['signal_date'],days[i+11])

    def test_unknown_signal_never_becomes_no_signal(self):
        days,_=fixture();row=signal('2026-01-02');row['abnormal']=None
        attempts,counts=schedule([row],days,'abnormal',10)
        self.assertFalse(attempts);self.assertEqual(counts['unknown_signal_days'],1)

    def test_price_dividend_proxy_and_benchmark_are_separate(self):
        days,books=fixture();attempts,_=schedule([signal('2026-01-02')],days,'abnormal',10)
        row=value_attempt(attempts[0],books,days)
        self.assertAlmostEqual(row['price_return'],0.02)
        self.assertAlmostEqual(row['dividend_adjusted_reference_return'],0.03)
        self.assertAlmostEqual(row['net_excess_over_benchmark'],0)
        self.assertAlmostEqual(row['max_adverse_price_return'],-0.05)

    def test_missing_holding_day_stays_unknown_and_blocks_later_position(self):
        days,books=fixture();i=days.index('2026-01-02')
        attempts,_=schedule([signal(days[i]),signal(days[i+15])],days,'abnormal',10)
        books['X'][days[i+3]]['valid']=False
        rows=evaluate(attempts,books,days)
        self.assertEqual(rows[0]['status'],'unresolved')
        self.assertEqual(rows[1]['reason'],'prior_position_state_unresolved')
        summary=summarize(rows)
        self.assertIsNone(summary['mean_net_return']);self.assertEqual(summary['unresolved'],2)

    def test_future_prices_do_not_change_scheduling(self):
        days,books=fixture();scored=[signal('2026-01-02'),signal('2026-01-06')]
        before=schedule(scored,days,'abnormal',10)
        books['X']['2026-01-06']['split']['c']=0.1
        self.assertEqual(before,schedule(scored,days,'abnormal',10))

    def test_repetition_pair_requires_wait_and_is_not_available_on_first_day(self):
        days,books=fixture();scored=[signal('2026-01-02'),signal('2026-01-06',True)]
        attempts,_=schedule(scored,days,'abnormal',10)
        result=wait_pairs(attempts,scored,books,days)
        self.assertEqual(result['n'],1);self.assertEqual(result['pairs'][0]['wait_sessions'],2)
        self.assertIn('conditional_on_future',result['limitation'])

    def test_all_winning_trades_do_not_emit_infinite_profit_factor(self):
        days,books=fixture();attempts,_=schedule([signal('2026-01-02')],days,'abnormal',10)
        result=summarize(evaluate(attempts,books,days))
        self.assertEqual(result['net_win_fraction'],1)
        self.assertIsNone(result['mean_loss']);self.assertIsNone(result['leave_one_symbol_out']['minimum_mean'])

    def test_changed_overlap_is_held_instead_of_rescoring_old_signals(self):
        row={'t':'2026-01-02T05:00:00Z','o':100,'h':105,'l':95,'c':102,'v':1000}
        data={k:{'X':[row]} for k in ('raw','split','split,dividend')}
        source={'X':{'raw':{'X':[{**row,'v':999}]}}}
        books,quality=prepare_books(data,[{'date':'2026-01-02'}],source,'2026-09-10')
        self.assertFalse(books['X']['2026-01-02']['valid'])
        self.assertEqual(quality[0]['original_signal_input_changed_dates'],['2026-01-02'])

    def test_split_adjusted_price_ratio_avoids_artificial_split_profit(self):
        days,books=fixture();attempts,_=schedule([signal('2026-01-02')],days,'abnormal',10)
        end=attempts[0]['exit_date'];books['X'][end]['raw']['c']=510
        # 株数5分の1と株価5倍を補正した後は、510/100という利益にはしない。
        self.assertAlmostEqual(value_attempt(attempts[0],books,days)['price_return'],0.02)


if __name__=='__main__':unittest.main()
