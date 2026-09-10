"""確認済みの判断が、別の数字や未確認事項へ広がらないことを検査する。"""
import copy
import unittest

from flow_probe.admission_review import (pilot_material_status, primary_confidential_status,
                                        review_queue, same_size_positions, verify_class_review)
from flow_probe.filing_review import object_hash


class AdmissionReviewTests(unittest.TestCase):
    def test_queue_does_not_depend_on_ratio_or_returns(self):
        row = {'denominator': {'shares': 100}, 'identity_verified': True,
               'historical_index_flags': [], 'holdings_quality': {'counts': {
                   'unresolved_filings': 0, 'confidential_status_unknown_managers': 4,
                   'confidential_omission_managers': 0}}}
        first = {**row, 'ratio': 1, 'future_return': -90}
        second = {**row, 'ratio': 90, 'future_return': 900}
        self.assertEqual(review_queue([first, second]), [first, second])
        bad = copy.deepcopy(row)
        bad['holdings_quality']['counts']['unresolved_filings'] = 1
        self.assertEqual(review_queue([bad]), [])

    def test_equal_positions_are_not_deducted_or_proven_duplicate(self):
        holdings = {'ledger': [{'cik': 'a', 'rows': [{'shares': 44286}, {'shares': 44286}]},
                              {'cik': 'b', 'rows': [{'shares': 44286}]}]}
        before = copy.deepcopy(holdings)
        result = same_size_positions(holdings)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['shares_each'], 44286)
        self.assertFalse(result[0]['overlap_confirmed'])
        self.assertEqual(result[0]['automatically_subtracted'], 0)
        self.assertEqual(before, holdings)

    def primary(self, indicator='', cik='1', period='09-30-2025'):
        return (f'<submission><credentials><cik>{cik}</cik></credentials>'
                f'<periodOfReport>{period}</periodOfReport>{indicator}</submission>').encode()

    def test_missing_original_indicator_does_not_become_false(self):
        self.assertEqual(primary_confidential_status(self.primary(), '0000000001')['status'], 'unknown')
        self.assertEqual(primary_confidential_status(self.primary('<isConfidentialOmitted>false</isConfidentialOmitted>'),
                                                    '0000000001')['status'], 'not_declared')

    def test_other_manager_or_period_cannot_resolve_current_flag(self):
        for body in [self.primary(cik='2'), self.primary(period='12-31-2025')]:
            with self.assertRaisesRegex(ValueError, 'identity_or_period'):
                primary_confidential_status(body, '0000000001')

    def test_review_withdrawn_if_input_or_balance_sheet_row_changes(self):
        row = {'cusip': '123456789', 'issuer_cik': '0000000001',
               'denominator': {'shares': 100, 'period': '2025-09-30', 'source': {'source_sha256': 'source'}}}
        identity = {'reviews': {'ownership_identity': {'source_sha256': 'ownership', 'security_class': 'Common'}}}
        document = '<html><tr><td>Common stock</td><td><number id="f">100</number></td></tr></html>'
        review = {'eligibility_row_sha256': object_hash(row), 'cusip': row['cusip'],
                  'issuer_cik': row['issuer_cik'], 'denominator_source_sha256': 'source',
                  'shares': 100, 'period': '2025-09-30', 'ownership_source_sha256': 'ownership',
                  'ownership_security_class': 'Common', 'fact_id': 'f',
                  'balance_sheet_row_sha256': object_hash('Common stock 100'), 'review_conclusion': 'same_class'}
        self.assertTrue(verify_class_review(row, identity, document, review)['security_class_verified'])
        with self.assertRaisesRegex(ValueError, 'row_changed'):
            verify_class_review(row, identity, document.replace('100', '200'), review)
        changed = copy.deepcopy(row); changed['denominator']['shares'] = 200
        with self.assertRaisesRegex(ValueError, 'input_changed'):
            verify_class_review(changed, identity, document, review)

    def test_pilot_material_is_not_low_ownership_or_accuracy_certification(self):
        row = {'holdings_quality': {'no_detected_aggregation_issues': True}}
        result = pilot_material_status(row, {'security_class_verified': True}, True, [])
        self.assertTrue(result['ready_for_reported_holdings_pilot_with_listing_proxy'])
        self.assertFalse(result['listing_at_selection_date_confirmed'])
        self.assertFalse(result['complete_institutional_ownership_confirmed'])
        self.assertIsNone(result['low_ownership_eligible'])
        self.assertFalse(result['eligible_for_accuracy_measurement'])
        self.assertFalse(pilot_material_status(row, {'security_class_verified': True}, True,
                                              [{'overlap_confirmed': False}])['ready_for_reported_holdings_pilot_with_listing_proxy'])


if __name__ == '__main__':
    unittest.main()
