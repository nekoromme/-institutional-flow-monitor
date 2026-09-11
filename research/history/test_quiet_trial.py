import unittest
from copy import deepcopy
from research.history import test_practical_validation as fixtures
from research.history.practical_validation import prepare
from research.history.development_validation import prepare_signals
from research.history.quiet_trial import quiet_signals
from research.history.direction_probe import classify

class QuietChecks(unittest.TestCase):
    def test_future_data_do_not_change_quiet_or_breakout_flags(self):
        p=fixtures.PracticalChecks().fixture();q=deepcopy(p)
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

class QuietSequenceChecks(unittest.TestCase):
    def test_quiet_candidate_waits_for_later_close_above_fixed_ceiling(self):
        from datetime import date,timedelta
        days=[(date(2023,1,1)+timedelta(days=i)).isoformat() for i in range(86)]
        book={d:{'valid':True,'split':{'h':10.2 if i<60 else 10.1,'l':9.8 if i<60 else 9.9,'c':10}} for i,d in enumerate(days)}
        book[days[-1]]['split'].update(h=10.3,c=10.2)
        rows=[{'symbol':'A','date':days[i],'repeated':True} for i in (84,85)]
        quiet_signals(rows,{'A':book},days)
        self.assertTrue(rows[0]['quiet_now']);self.assertFalse(rows[0]['quiet_breakout'])
        self.assertTrue(rows[1]['quiet_breakout'])

class RegularConditionChecks(unittest.TestCase):
    def test_cts_space_regular_trade_is_not_rejected(self):
        t=[{'t':'2023-01-02T15:00:00.100Z','p':10,'s':100,'c':[' ','F']}]
        q=[{'t':'2023-01-02T15:00:00Z','bp':9.99,'ap':10,'bs':1,'as':1}]
        self.assertEqual(classify(t,q)['edge_buy_fraction'],1)
