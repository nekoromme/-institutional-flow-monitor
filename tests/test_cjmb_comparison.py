"""後から得た情報の混入、欠損を売却と呼ぶ誤り、修正前後の逆転を検査。"""
import copy
import unittest

from flow_probe.cjmb_comparison import (START_PERIOD, END_PERIOD, compare_ratios,
                                       reported_change, verify_share_bridge, vintage_changes)
from flow_probe.filing_review import object_hash
from flow_probe.identity_sources import source_documents
from test_identity import body, source
from test_holdings import row


def holdings(positions, cusip='131100109'):
    return {'cusip': cusip, 'reported_share_sum_before_overlap_resolution': sum(positions.values()),
            'ledger': [{'cik': c, 'name': c, 'shares': n} for c, n in positions.items()]}


class ComparisonTests(unittest.TestCase):
    def test_outcome_cutoff_does_not_relax_selection_default(self):
        content = body('<html/>', '10-Q', accepted='20260515160000').replace(b'20251110', b'20260515')
        descriptor = source('10-Q', filed='2026-05-15')
        with self.assertRaisesRegex(ValueError, 'original_header_or_time_mismatch'):
            source_documents(content, descriptor)
        self.assertEqual(source_documents(content, descriptor, as_of='2026-06-01')[2], '20260515160000')
        with self.assertRaisesRegex(ValueError, 'original_header_or_time_mismatch'):
            source_documents(content, descriptor, as_of='2026-05-15')
        # 明示的な呼び出しの後も、共有の既定値は変わっていない。
        with self.assertRaises(ValueError):
            source_documents(content, descriptor)

    def test_absence_is_not_reported_as_zero_holding_or_trade(self):
        result = reported_change(holdings({'a': 100, 'b': 50}), holdings({'a': 120, 'c': 10}))
        self.assertEqual(result['diagnostic_reported_share_change'], -20)
        self.assertEqual(result['decomposition'], {'both': 20, 'previous_only': -50, 'current_only': 10})
        row = next(r for r in result['manager_records'] if r['cik'] == 'b')
        self.assertIsNone(row['current_reported_shares'])
        self.assertFalse(row['actual_trade_change_confirmed'])
        self.assertIsNone(result['institutional_flow_label'])
        self.assertFalse(result['eligible_for_accuracy_measurement'])

    def test_restoring_excluded_baseline_can_reverse_apparent_increase(self):
        end = holdings({'a': 600})
        self.assertGreater(reported_change(holdings({'a': 500}), end)['diagnostic_reported_share_change'], 0)
        self.assertLess(reported_change(holdings({'a': 500, 'b': 200}), end)['diagnostic_reported_share_change'], 0)

    def test_share_increase_can_coexist_with_ratio_decline(self):
        denominator = {START_PERIOD: {'shares': 1000}, END_PERIOD: {'shares': 2000}}
        result = compare_ratios(holdings({'a': 100}), holdings({'a': 150}), denominator, unit_reviewed=True)
        self.assertEqual(result['previous_ratio_percent'], 10)
        self.assertEqual(result['current_ratio_percent'], 7.5)
        self.assertEqual(result['ratio_change_percentage_points'], -2.5)
        self.assertIsNone(result['actual_institutional_ownership_percent'])

    def test_unreviewed_unit_does_not_produce_ratio_change(self):
        result = compare_ratios(holdings({'a': 100}), holdings({'a': 150}), {}, unit_reviewed=False)
        self.assertIsNone(result['ratio_change_percentage_points'])

    def test_different_security_or_inconsistent_ledger_rejected(self):
        with self.assertRaisesRegex(ValueError, 'different_security'):
            reported_change(holdings({'a': 100}), holdings({'a': 100}, '999999999'))
        changed = holdings({'a': 100}); changed['reported_share_sum_before_overlap_resolution'] = 99
        with self.assertRaisesRegex(ValueError, 'inconsistent_manager_ledger'):
            reported_change(changed, holdings({'a': 100}))

    def test_equity_bridge_requires_original_rows_and_reconciliation(self):
        texts = ['beginning 100', 'issued 20', 'vested 5', 'ending 125']
        bridge = {'rows': texts, 'row_sha256': [object_hash(t) for t in texts],
                  'beginning_shares': 100, 'equity_line_issued_shares': 20,
                  'vested_award_shares': 5, 'ending_shares': 125, 'reviewed_unit_multiplier': 1}
        self.assertTrue(verify_share_bridge(bridge, texts))
        with self.assertRaisesRegex(ValueError, 'original_rows_changed'):
            verify_share_bridge(bridge, texts[:-1])
        changed = copy.deepcopy(bridge); changed['ending_shares'] = 120
        with self.assertRaisesRegex(ValueError, 'does_not_reconcile'):
            verify_share_bridge(changed, texts)

    def test_later_accession_is_not_necessarily_a_position_change(self):
        early = holdings({'a': 100})
        early['ledger'][0].update(accessions=['original'], rows=[row(100)])
        late = copy.deepcopy(early)
        late['ledger'][0]['accessions'] = ['amendment']
        late['ledger'][0]['rows'][0]['accession'] = 'amendment'
        self.assertTrue(vintage_changes(early, late)[0]['same_reported_position_payloads'])
        late['ledger'][0]['rows'][0]['shares'] = 200
        self.assertFalse(vintage_changes(early, late)[0]['same_reported_position_payloads'])


if __name__ == '__main__':
    unittest.main()
