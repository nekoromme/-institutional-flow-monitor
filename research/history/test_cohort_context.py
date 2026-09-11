import unittest
from research.history.cohort_context_trial import peer_median, prior_returns

class PeerTests(unittest.TestCase):
    def test_self_excluded_and_minimum_checked_after_exclusion(self):
        values={'self': 100, 'a': -.1, 'b': .1}
        self.assertEqual(peer_median(values, 'self', 2), 0)
        self.assertIsNone(peer_median(values, 'self', 3))

    def test_future_prices_do_not_change_prior_return(self):
        days=[f'{i:03d}' for i in range(23)]
        book={d: {'valid': True, 'split': {'c': 100+i}} for i,d in enumerate(days)}
        result=prior_returns({'a':book}, days, 20)
        book[days[21]]['split']['c']=99999
        self.assertEqual(result, prior_returns({'a':book}, days, 20))
        book[days[10]]['valid']=False
        self.assertEqual(prior_returns({'a':book},days,20),{})
