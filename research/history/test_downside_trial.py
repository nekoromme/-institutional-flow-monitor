import unittest
from research.history.downside_trial import upside_allowed

class DownsideChecks(unittest.TestCase):
    def test_large_decline_does_not_exclude(self):
        self.assertTrue(upside_allowed([{'h':100,'l':90},{'h':80,'l':60}],100))
    def test_intraday_rally_is_excluded_even_if_price_later_falls(self):
        self.assertFalse(upside_allowed([{'h':106,'l':100},{'h':80,'l':60}],100))
    def test_boundary_is_inclusive_and_unit_invariant(self):
        self.assertTrue(upside_allowed([{'h':105}],100));self.assertTrue(upside_allowed([{'h':10.5}],10))
