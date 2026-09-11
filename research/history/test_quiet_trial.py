import unittest
from copy import deepcopy
from research.history.test_practical_validation import PracticalChecks
from research.history.practical_validation import prepare
from research.history.development_validation import prepare_signals
from research.history.quiet_trial import quiet_signals
from research.history.direction_probe import classify

class QuietChecks(unittest.TestCase):
    def test_future_data_do_not_change_quiet_or_breakout_flags(self):
        p=PracticalChecks().fixture();q=deepcopy(p)
        for a in q['prices']:
            for b in q['prices'][a]['TEST'][95:]:b.update(o=100,h=110,l=90,c=100,v=99999999)
        def flags(payload):
            book,_=prepare(payload);s,d=prepare_signals(payload,book,[],2023);quiet_signals(s,book,d)
            return [(r['date'],r['quiet_now'],r['quiet_breakout']) for r in s if r['date']<p['sessions'][95]['date']]
        self.assertEqual(flags(p),flags(q))
    def test_same_price_can_be_buy_or_sell_depending_on_quote(self):
        t=[{'t':'2023-01-02T15:00:00.100Z','p':10,'s':100,'c':['@']},{'t':'2023-01-02T15:00:01.100Z','p':10,'s':200,'c':['@']}]
        q=[{'t':'2023-01-02T15:00:00Z','bp':9.99,'ap':10,'bs':1,'as':1},{'t':'2023-01-02T15:00:01Z','bp':10,'ap':10.01,'bs':1,'as':1}]
        r=classify(t,q);self.assertEqual(r['share_volume']['buy'],100);self.assertEqual(r['share_volume']['sell'],200)
    def test_future_and_simultaneous_quotes_are_not_used(self):
        t=[{'t':'2023-01-02T15:00:00Z','p':10,'s':100,'c':['@']}]
        q=[{'t':t[0]['t'],'bp':9.99,'ap':10,'bs':1,'as':1}]
        self.assertEqual(classify(t,q)['share_volume']['no_prior_quote'],100)
    def test_stale_and_midpoint_trades_remain_unclassified(self):
        q=[{'t':'2023-01-02T15:00:00Z','bp':9.99,'ap':10.01,'bs':1,'as':1}]
        t=[{'t':'2023-01-02T15:00:00.100Z','p':10,'s':100,'c':['@']},{'t':'2023-01-02T15:00:02Z','p':10.01,'s':200,'c':['@']}]
        r=classify(t,q);self.assertEqual(r['share_volume']['inside_spread'],100);self.assertEqual(r['share_volume']['stale_quote'],200);self.assertIsNone(r['edge_buy_fraction'])
