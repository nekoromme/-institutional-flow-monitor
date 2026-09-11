"""日中高値で売れたと仮定せず、費用と利益の取り逃しを区別する。"""
import unittest
from research.history.failure_diagnostics import describe

class FailureChecks(unittest.TestCase):
    def sample(self,closes,highs,price_return=-.02,net_return=-.025):
        days=['2023-01-02','2023-01-03'];books={'A':{d:{'split':{'o':100,'c':c,'h':h}} for d,c,h in zip(days,closes,highs)}}
        t={'symbol':'A','entry_date':days[0],'exit_date':days[-1],'status':'priced','price_return':price_return,'net_return':net_return}
        return describe(t,books,days)
    def test_high_only_gain_is_not_close_gain(self):
        self.assertEqual(self.sample([99,98],[102,100])['failure_shape'],'intraday_gain_only_then_loss')
    def test_close_gain_then_loss(self):
        self.assertEqual(self.sample([102,98],[103,100])['failure_shape'],'profitable_close_then_loss')
    def test_cost_only_loss(self):
        self.assertEqual(self.sample([100.1,100.2],[100.2,100.3],.002,-.003)['failure_shape'],'cost_erased_gain')
