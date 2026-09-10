"""株数の桁・日付・会社・種類を取り違えて、保有率を作らないための検査。"""
import unittest

from flow_probe.share_facts import outstanding_candidates
from flow_probe.eligibility import selection_row
from flow_probe.bulk13f import confidential_status, select_filings
from flow_probe.holdings import aggregate_security
from test_holdings import filing, row

CIK = '0000000001'


def context(name='c', cik=CIK, day='2025-09-30', dimension=False):
    segment = ('<xbrli:segment><xbrldi:explicitMember dimension="s:ClassAxis">s:ClassA</xbrldi:explicitMember></xbrli:segment>' if dimension else '')
    return f'<xbrli:context id="{name}"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">{cik}</xbrli:identifier>{segment}</xbrli:entity><xbrli:period><xbrli:instant>{day}</xbrli:instant></xbrli:period></xbrli:context>'


def fact(number='52,383', name='c', scale='3', unit='shares', tag='CommonStockSharesOutstanding', extra=''):
    return f'<ix:nonFraction name="us-gaap:{tag}" contextRef="{name}" unitRef="{unit}" format="ixt:num-dot-decimal" scale="{scale}" decimals="-3" {extra}>{number}</ix:nonFraction>'


def document(content):
    return '<html xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" xmlns:us-gaap="http://fasb.org/us-gaap/2025" xmlns:ixt="http://www.xbrl.org/inlineXBRL/transformation/2020-02-12" xmlns:s="https://example.com/test"><xbrli:unit id="shares"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit><xbrli:unit id="dollars"><xbrli:measure>s:USD</xbrli:measure></xbrli:unit>' + content + '</html>'


class ShareFactTests(unittest.TestCase):
    def test_scale_is_not_decimals_and_future_cover_count_is_excluded(self):
        text = document(context() + context('later', day='2025-11-03') + fact() + fact('999', 'later'))
        result = outstanding_candidates(text, CIK)
        self.assertEqual(result['shares'], 52_383_000)
        self.assertEqual(result['facts'][0]['rounded_to_shares'], 1000)
        self.assertEqual(result['rejected_fact_reasons']['not_target_period'], 1)
        self.assertFalse(result['denominator_certified'])

    def test_dollars_and_issued_shares_do_not_become_outstanding_shares(self):
        text = document(context() + fact(unit='dollars') + fact(tag='CommonStockSharesIssued'))
        result = outstanding_candidates(text, CIK)
        self.assertIsNone(result['shares'])
        self.assertEqual(result['rejected_fact_reasons']['not_share_units'], 1)

    def test_joint_report_uses_context_entity_not_first_number(self):
        text = document(context('child', cik='0000000002') + context() + fact('999', 'child') + fact())
        result = outstanding_candidates(text, CIK)
        self.assertEqual(result['shares'], 52_383_000)
        self.assertEqual(result['rejected_fact_reasons']['context_issuer_mismatch'], 1)

    def test_class_specific_facts_not_summed_into_denominator(self):
        result = outstanding_candidates(document(context(dimension=True) + fact()), CIK)
        self.assertIsNone(result['shares'])
        self.assertIn('dimensional_share_fact_requires_class_review', result['rejected_fact_reasons'])

    def test_conflicting_period_values_and_duplicate_context_fail_closed(self):
        result = outstanding_candidates(document(context() + fact() + fact('10')), CIK)
        self.assertEqual(result['status'], 'conflicting_period_facts')
        self.assertIsNone(result['shares'])
        with self.assertRaisesRegex(ValueError, 'duplicate_or_missing_context_or_unit_id'):
            outstanding_candidates(document(context() + context() + fact()), CIK)

    def test_invalid_sign_format_and_nested_facts_are_not_guessed(self):
        samples = [fact(extra='sign="-"'), fact('52,38'),
                   fact().replace('ixt:num-dot-decimal', 'ixt:unknown'),
                   fact('<ix:nonFraction name="s:other">52,383</ix:nonFraction>')]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertIsNone(outstanding_candidates(document(context() + sample), CIK)['shares'])

    def test_custom_namespace_cannot_impersonate_standard_fact(self):
        text = document(context() + fact()).replace('http://fasb.org/us-gaap/2025', 'https://example.com/fake')
        result = outstanding_candidates(text, CIK)
        self.assertIsNone(result['shares'])
        self.assertIn('nonstandard_outstanding_tag', result['rejected_fact_reasons'])


class EligibilityTests(unittest.TestCase):
    def holding(self):
        return {'reported_share_sum_before_overlap_resolution': 150,
                'positive_reporting_managers': 2, 'unresolved_filings': [],
                'potential_overlap_relationships': [], 'unresolved_manager_references': [],
                'repeated_row_payloads': [], 'unreviewed_class_rows': [],
                'confidential_omission_managers': [], 'confidential_status_unknown_managers': []}

    def identity(self):
        return {'ordinal': 1, 'cusip': '000000001', 'issuer': 'Example', 'issuer_candidate_cik': CIK,
                'identity_verified': True, 'ticker_in_reviewed_filing': 'ABC', 'review_flags': []}

    def test_ratio_above_100_is_not_capped_or_certified(self):
        result = selection_row(self.identity(), self.holding(), {'status': 'exact_period_numeric_candidate', 'shares': 100})
        self.assertEqual(result['diagnostic_reported_sum_divided_by_candidate_shares_percent'], 150)
        self.assertIn('diagnostic_ratio_exceeds_100_percent_requires_review', result['review_reasons'])
        self.assertIn('listing_at_selection_date_not_confirmed', result['review_reasons'])
        self.assertIsNone(result['institutional_ownership_percent'])
        self.assertIsNone(result['low_ownership_eligible'])
        self.assertFalse(result['eligible_for_backtest'])

    def test_unknown_identity_or_denominator_never_turns_into_zero_ratio(self):
        identity = {**self.identity(), 'identity_verified': False, 'ticker_in_reviewed_filing': None}
        result = selection_row(identity, self.holding(), {'status': 'identity_not_ready', 'shares': None})
        self.assertIsNone(result['diagnostic_reported_sum_divided_by_candidate_shares_percent'])
        holding = self.holding(); holding['unresolved_manager_references'] = [{'cik': 'unresolved'}]
        result = selection_row(self.identity(), holding, {'status': 'exact_period_numeric_candidate', 'shares': 1000})
        self.assertIn('reported_holdings_aggregation_requires_review', result['review_reasons'])
        self.assertFalse(result['holdings_quality']['complete_institutional_coverage_confirmed'])

    def test_blank_confidential_status_is_unknown_and_does_not_change_shares(self):
        self.assertIsNone(confidential_status(''))
        self.assertIsNone(confidential_status('unexpected'))
        self.assertIs(confidential_status('Y'), True)
        self.assertIs(confidential_status('N'), False)
        f = filing('original', [row(100)], filed='2025-11-14', period='2025-09-30')
        f['confidential_omitted'] = None
        states = select_filings({'original': f}, '2025-09-30', '2026-01-01')
        result = aggregate_security(states, '00848K309')
        self.assertEqual(result['reported_share_sum_before_overlap_resolution'], 100)
        self.assertEqual(result['confidential_omission_managers'], [])
        self.assertEqual(len(result['confidential_status_unknown_managers']), 1)


if __name__ == '__main__':
    unittest.main()
