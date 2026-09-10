import copy
import unittest

from flow_probe.pilot import classify, score, summarize
from flow_probe.uncertainty import triage


class PilotTests(unittest.TestCase):
    def row(self, day='2026-01-02', v=58):
        return {'symbol': 'X', 'date': day, 'status': 'prepared', 'raw_volume': v,
                'baseline_volumes_in_decision_day_shares': list(range(1, 61))}

    def test_nearest_rank_strict_tie(self):
        self.assertFalse(classify(self.row(v=57))['abnormal'])
        result = classify(self.row(v=58))
        self.assertTrue(result['abnormal'])
        self.assertEqual(result['threshold_volume'], 57)
        self.assertEqual(result['volume_multiple'], 58/30.5)

    def test_invalid_values_are_unknown(self):
        for value in [0, -1, True, None, float('nan'), float('inf')]:
            self.assertIsNone(classify(self.row(v=value))['abnormal'])
        row = self.row()
        row['baseline_volumes_in_decision_day_shares'][0] = 0
        self.assertIsNone(classify(row)['abnormal'])

    def test_repeat_uses_sessions_across_weekend(self):
        days = ['2025-12-26', '2025-12-29', '2025-12-30', '2025-12-31', '2026-01-02']
        rows = [self.row(d, 58 if i in (0, 4) else 10) for i, d in enumerate(days)]
        result = score(rows, days)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]['repeated'])
        rows[-1]['raw_volume'] = 10
        self.assertFalse(score(rows, days)[0]['repeated'])

    def test_missing_and_short_sessions_do_not_compress_window(self):
        days = ['2025-12-26', '2025-12-29', '2025-12-30', '2025-12-31', '2026-01-02']
        rows = [self.row(d) for d in days]
        self.assertIsNone(score(rows[1:], days)[0]['repeated'])
        rows[0]['status'] = 'shortened_session'
        self.assertIsNone(score(rows, days)[0]['repeated'])

    def test_future_and_holdings_cannot_change_signal(self):
        days = ['2026-03-30', '2026-03-31', '2026-04-01']
        rows = [self.row(d) for d in days]
        before = score(rows, days)
        rows[-1]['raw_volume'] = 999999
        self.assertEqual(score(rows, days), before)
        self.assertEqual([r['date'] for r in before], days[:2])

    def test_duplicate_and_noncalendar_rejected(self):
        row = self.row()
        with self.assertRaises(ValueError):
            score([row, row], [row['date']])
        with self.assertRaises(ValueError):
            score([row], [])

    def test_unknown_label_never_becomes_miss_or_zero(self):
        item = {'reported_share_sum_before_overlap_resolution': 100,
                'unresolved_manager_references_count': 0, 'potential_overlap_relationships_count': 0,
                'repeated_row_payloads_count': 0, 'unresolved_filings_count': 0,
                'unreviewed_class_rows_count': 0}
        holdings = {'as_of': '2026-06-01', 'quarters': {
            '2025-12-31': {'symbols': {'X': item}},
            '2026-03-31': {'symbols': {'X': {**item, 'reported_share_sum_before_overlap_resolution': 200}}}}}
        scored = score([self.row()], ['2026-01-02'])
        out = summarize(scored, holdings)
        self.assertIsNone(out['accuracy'])
        self.assertIsNone(out['by_symbol']['X']['institutional_flow_label'])
        self.assertFalse(out['by_symbol']['X']['eligible_for_accuracy_measurement'])
        self.assertEqual(out['by_symbol']['X']['diagnostic_reported_share_change'], 100)
        self.assertNotIn('raw_volume', str(out))
        self.assertNotIn('threshold_volume', str(out))
        self.assertNotIn('volume_multiple', str(out))

    def test_triage_cover_is_not_target_link_and_manager_count_is_unique(self):
        item = {'unresolved_manager_references': [
            {'cik': '1', 'accession': 'A', 'relationship': 'reported_by'},
            {'cik': '1', 'accession': 'A', 'relationship': 'included_manager'}],
            'potential_overlap_relationships': [{'ciks': ['1', '2']}],
            'repeated_row_payloads': [{'cik': '1'}], 'unresolved_filings': [], 'unreviewed_class_rows': []}
        audit = {'as_of': '2026-06-01', 'quarters': {'2026-03-31': {'symbols': {'X': item}}}}
        ledgers = {'2026-03-31/X': [{'cik': '1', 'name': 'one', 'shares': 100},
                                     {'cik': '2', 'name': 'two', 'shares': 50}]}
        before = copy.deepcopy(audit)
        out = triage(audit, ledgers)['quarters']['2026-03-31']['X']
        self.assertEqual(out['associated_manager_shares_counted_once'], 150)
        self.assertFalse(out['associated_shares_are_error_bound'])
        self.assertEqual({x['kind'] for x in out['reference_review_queue']},
                         {'cover_reference_target_link_unproven', 'holding_row_reference'})
        self.assertEqual(audit, before)


if __name__ == '__main__':
    unittest.main()
