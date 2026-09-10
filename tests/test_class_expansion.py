"""株数の可視表と埋め込み値の食い違い、確認範囲の拡大解釈を防ぐ。"""
import copy
import unittest
from flow_probe.class_expansion import inventory, normalized_text, review_queue, verify_review
from flow_probe.filing_review import object_hash
from flow_probe.share_facts import parse_document, split_tag
from test_eligibility import CIK, context, document, fact


def fixture(separate=False):
    numeric = fact('100', scale='0', extra='id="f"')
    cell = '100' if separate else numeric
    text = document(context() + ('<ix:hidden>' + numeric + '</ix:hidden>' if separate else '')
                    + '<table><tr><td>September 30, 2025</td></tr>'
                    + '<tr><td>Common stock issued and outstanding</td><td>' + cell + '</td></tr></table>')
    tree, _ = parse_document(text)
    table = next(e for e in tree.iter() if split_tag(e.tag)[1] == 'table')
    tr = [e for e in table.iter() if split_tag(e.tag)[1] == 'tr'][1]
    source = {'source_sha256': 'original', 'filed': '2025-11-01'}
    row = {'ordinal': 3, 'cusip': '123456789', 'issuer_cik': CIK,
           'denominator': {'shares': 100, 'source': source}}
    identity = {'reviews': {'ownership_identity': {'source_sha256': 'ownership', 'security_class': 'Common'}}}
    review = {'ordinal': 3, 'cusip': row['cusip'], 'issuer_cik': CIK,
              'eligibility_row_sha256': object_hash(row), 'source': source,
              'shares': 100, 'period': '2025-09-30', 'fact_id': 'f', 'rounding_to_shares': 1000,
              'ownership_source_sha256': 'ownership', 'ownership_security_class': 'Common',
              'balance_sheet_row_excerpt': normalized_text(tr),
              'balance_sheet_row_sha256': object_hash(normalized_text(tr)),
              'balance_sheet_table_sha256': object_hash(normalized_text(table)),
              'fact_location': 'separate_numeric_fact_and_visible_reviewed_row' if separate else 'inside_reviewed_row'}
    return row, identity, text, review


class ClassExpansionTests(unittest.TestCase):
    def test_separate_numeric_and_visible_row_both_verified(self):
        for separate in [False, True]:
            with self.subTest(separate=separate):
                result = verify_review(*fixture(separate))
                self.assertTrue(result['security_class_verified'])
                self.assertFalse(result['complete_institutional_ownership_confirmed'])

    def test_visible_value_cannot_disagree_with_unchanged_hidden_fact(self):
        row, identity, text, review = fixture(True)
        text = text.replace('<td>100</td>', '<td>200</td>')
        with self.assertRaisesRegex(ValueError, 'visible_row'):
            verify_review(row, identity, text, review)

    def test_hidden_fact_company_or_date_change_blocks_review(self):
        row, identity, text, review = fixture(True)
        for altered in [text.replace(CIK, '0000000002'), text.replace('2025-09-30', '2025-12-31')]:
            with self.subTest(altered=altered):
                with self.assertRaisesRegex(ValueError, 'numeric_fact_changed'):
                    verify_review(row, identity, altered, review)

    def test_table_date_change_cannot_reuse_same_row(self):
        row, identity, text, review = fixture()
        with self.assertRaisesRegex(ValueError, 'table_or_dates_changed'):
            verify_review(row, identity, text.replace('September 30, 2025', 'December 31, 2025'), review)

    def test_duplicate_visible_row_is_ambiguous(self):
        row, identity, text, review = fixture(True)
        tr = '<tr><td>Common stock issued and outstanding</td><td>100</td></tr>'
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            verify_review(row, identity, text.replace(tr, tr + tr), review)

    def test_rounding_precision_and_input_change_invalidate_review(self):
        row, identity, text, review = fixture()
        with self.assertRaisesRegex(ValueError, 'precision_changed'):
            verify_review(row, identity, text.replace('decimals="-3"', 'decimals="INF"'), review)
        changed = copy.deepcopy(row); changed['denominator']['shares'] = 200
        with self.assertRaisesRegex(ValueError, 'input_changed'):
            verify_review(changed, identity, text, review)

    def test_selection_order_ignores_quality_ratio_and_outcomes(self):
        rows = [{'ordinal': n, 'denominator': {'shares': 100}, 'ratio': n,
                 'quality_flags': n, 'future_return': -n} for n in range(1, 20)]
        before = [r['ordinal'] for r in review_queue(rows)]
        for r in rows:
            r.update(ratio=10000, quality_flags=0, future_return=10000)
        self.assertEqual(before, [r['ordinal'] for r in review_queue(list(reversed(rows)))])
        self.assertNotIn(4, before)
        self.assertEqual(len(before), 10)

    def test_class_review_does_not_remove_other_warnings_or_admit_sample(self):
        row = {'ordinal': 3, 'cusip': '123456789', 'ticker_in_historical_filing': 'ABC',
               'denominator': {'shares': 100}, 'review_reasons': [
                   'denominator_security_class_not_certified', 'reported_holdings_aggregation_requires_review',
                   'listing_at_selection_date_not_confirmed']}
        result = inventory([row], {3})[0]
        self.assertEqual(result['class_status'], 'verified')
        self.assertEqual(len(result['remaining_review_reasons']), 2)
        self.assertIsNone(result['low_ownership_eligible'])
        self.assertFalse(result['eligible_for_accuracy_measurement'])


if __name__ == '__main__':
    unittest.main()
