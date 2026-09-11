"""境界と当日情報だけの判定を、小さな人工価格で確認する。"""
import unittest
from research.history.reversal_trial import mechanism_flags

class MechanismTests(unittest.TestCase):
    def test_declining_day_can_recover_from_low(self):
        # 過去価格100より下落していても、高安の中点なら回復条件を満たす。
        self.assertEqual(mechanism_flags({'h': 96, 'l': 80, 'c': 88}, [100]*60), (True, False))

    def test_new_low_without_recovery_is_not_absorption(self):
        self.assertEqual(mechanism_flags({'h': 96, 'l': 80, 'c': 81}, [100]*60), (False, False))

    def test_no_range_does_not_claim_recovery(self):
        self.assertEqual(mechanism_flags({'h': 100, 'l': 100, 'c': 100}, [100]*60), (False, True))

    def test_insufficient_trend_history(self):
        self.assertFalse(mechanism_flags({'h': 102, 'l': 98, 'c': 100}, [90]*59)[1])

    def test_split_unit_invariance(self):
        b={'h': 96, 'l': 80, 'c': 88}
        self.assertEqual(mechanism_flags(b,[85]*60), mechanism_flags({k:v/20 for k,v in b.items()},[85/20]*60))
