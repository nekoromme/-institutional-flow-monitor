import unittest
from research.history.early_exit_trial import exit_variant
from flow_probe.returns_core import after_cost

class EarlyExitChecks(unittest.TestCase):
    def test_decision_at_close_executes_next_open_with_gap(self):
        days=['2023-01-02','2023-01-03','2023-01-04']
        b={'A':{days[0]:{'split':{'o':100,'c':99,'l':98}},days[1]:{'split':{'o':80,'c':120,'l':50}}}}
        t={'status':'priced','symbol':'A','entry_date':days[0],'exit_date':days[2],'net_return':.2}
        r=exit_variant(t,b,days)
        self.assertAlmostEqual(r['net_return'],after_cost(.8,.0025));self.assertEqual(r['reserved_through'],days[2])
        self.assertAlmostEqual(r['max_adverse_price_return'],-.2)
    def test_no_exit_on_flat_entry_day(self):
        b={'A':{'2023-01-02':{'split':{'o':100,'c':100}}}}
        t={'status':'priced','symbol':'A','entry_date':'2023-01-02','exit_date':'2023-01-04','net_return':.2}
        r=exit_variant(t,b,['2023-01-02']);self.assertFalse(r['early_exit']);self.assertEqual(r['net_return'],.2)
