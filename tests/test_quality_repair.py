"""補正修正が未来情報を混ぜないことと、0を安易に採用しないことを確認。"""
from datetime import date, datetime, timedelta
import unittest

from flow_probe.alpaca import NY, split_adjustment_audit
from flow_probe.quality_repair import zero_assessment
from flow_probe.reference import documented_price_factor
from flow_probe.research import prepare_daily


class QualityRepairTests(unittest.TestCase):
    def test_later_split_fixes_audit_but_not_historical_volume_units(self):
        start = date(2025, 9, 1)
        days = [(start+timedelta(days=i)).isoformat() for i in range(120)
                if (start+timedelta(days=i)).weekday() < 5]
        sessions = [{'date':d, 'open':'09:30', 'close':'16:00'} for d in days]
        # 正午UTCなら夏冬のどちらでも同じニューヨーク日付になる。
        raw = [{'t':d+'T12:00:00Z', 'o':1, 'h':1, 'l':1, 'c':1, 'v':1000} for d in days]
        adj = [{**r, 'o':5, 'h':5, 'l':5, 'c':5, 'v':200} for r in raw]
        old = split_adjustment_audit({'SMSI':raw}, {'SMSI':adj}, complete=True, through='2026-03-31')
        new = split_adjustment_audit({'SMSI':raw}, {'SMSI':adj}, complete=True, through='2026-09-10')
        self.assertEqual(old['SMSI']['status'], 'needs_review')
        self.assertEqual(new['SMSI']['status'], 'comparable_sample')
        prepared, _ = prepare_daily({'SMSI':raw}, sessions, new)
        self.assertEqual(prepared[-1]['baseline_volumes_in_decision_day_shares'], [1000]*60)
        self.assertEqual(documented_price_factor('SMSI','2026-01-05','2026-03-31'), 1)
        self.assertEqual(documented_price_factor('SMSI','2026-01-05','2026-06-05'), 5)

    def test_price_discrepancy_is_not_waived_when_volume_scale_agrees(self):
        raw = {'t':'2026-01-05T05:00:00Z','o':1,'h':1,'l':1,'c':1,'v':700}
        adj = {**raw,'o':7,'h':7,'l':7,'c':7.1,'v':100}
        q = split_adjustment_audit({'KAPA':[raw]}, {'KAPA':[adj]}, complete=True, through='2026-09-10')['KAPA']
        self.assertEqual(q['status'],'needs_review')
        self.assertEqual(q['volume_mismatch_days'],0)
        self.assertEqual(q['price_mismatch_days'],1)

    def zero(self, trades=(), minute=(), complete=True, daily=None):
        start=datetime(2026,1,5,tzinfo=NY)
        if daily is None: daily=[{'t':start.isoformat(),'v':0}]
        return zero_assessment(daily, list(minute), list(trades),
                               [{'complete':complete}]*3, start, start+timedelta(days=1))

    def test_regular_trade_proves_zero_conflict_not_replacement(self):
        result=self.zero([{'t':'2026-01-05T15:00:00Z','s':100,'c':['@']}])
        self.assertIn('conflicts_with_positive',result['conclusion'])
        self.assertEqual(result['regular_positive_trade_rows'],1)
        self.assertFalse(result['automatic_replacement_performed'])

    def test_empty_response_does_not_certify_zero(self):
        result=self.zero()
        self.assertIn('no_trade_not_certified',result['conclusion'])
        self.assertFalse(result['zero_accepted_as_valid_observation'])

    def test_partial_or_next_day_response_cannot_confirm_zero(self):
        trade={'t':'2026-01-06T05:00:00Z','s':100,'c':['@']}
        self.assertFalse(self.zero([trade])['complete'])
        self.assertFalse(self.zero(complete=False)['complete'])

    def test_missing_daily_bar_stays_missing(self):
        result=self.zero(daily=[])
        self.assertIn('missing_or_duplicate',result['conclusion'])
        self.assertEqual(result['daily_zero_rows'],0)


if __name__=='__main__': unittest.main()
