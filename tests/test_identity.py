"""名前の似た会社・保有者と発行者・普通株と社債を混同しない検査。"""
import unittest
import hashlib
from unittest.mock import patch

from flow_probe.identity import name_key, propose
from flow_probe.identity_sources import ownership_identity, listing_cover, listing_cover_parts, removal_notice, source_documents

CIK, CUSIP = '0000000001', '00848K309'


def source(form='SCHEDULE 13G', filed='2025-11-10'):
    return {'filename': 'edgar/data/1/0000000002-25-000001.txt', 'form': form, 'filed': filed}


def body(content, form='SCHEDULE 13G', *, tagged=False, accepted='20251110160000'):
    header = (f'<ACCESSION-NUMBER>0000000002-25-000001\n<TYPE>{form}\n<FILING-DATE>20251110\n' if tagged else
              f'ACCESSION NUMBER: 0000000002-25-000001\nCONFORMED SUBMISSION TYPE: {form}\nFILED AS OF DATE: 20251110\n')
    header = f'<SEC-HEADER>example\n<ACCEPTANCE-DATETIME>{accepted}\n{header}</SEC-HEADER>'
    return f'{header}<DOCUMENT>\n<TYPE>{form}\n<TEXT>{content}</TEXT></DOCUMENT>'.encode()


def ownership_xml(*, cik=CIK, cusip=CUSIP, upper=False, missing=False):
    cik_tag, cusip_tag = ('issuerCIK', 'issuerCUSIP') if upper else ('issuerCik', 'issuerCusip')
    ids = '' if missing else f'<{cik_tag}>{cik}</{cik_tag}><{cusip_tag}>{cusip}</{cusip_tag}>'
    return f'<XML><edgarSubmission><filerCredentials><cik>0000000002</cik></filerCredentials><issuerInfo>{ids}<issuerName>Example</issuerName></issuerInfo><securitiesClassTitle>Common Stock</securitiesClassTitle></edgarSubmission></XML>'


def fact(name, value, context='common'):
    return f'<ix:nonNumeric name="dei:{name}" contextRef="{context}">{value}</ix:nonNumeric>'


def cover(extra=''):
    return (fact('EntityCentralIndexKey', CIK) + fact('Security12bTitle', 'Common Stock') +
            fact('TradingSymbol', 'ABC') + fact('SecurityExchangeName', 'Nasdaq') + extra)


class IdentityTests(unittest.TestCase):
    def test_name_lookup_never_proves_identity(self):
        batch = [{'cusip': CUSIP, 'issuer': 'EXAMPLE HLDGS INC'}]
        rows = propose(batch, {CIK: {'Example Holdings Inc.'}}, {})
        self.assertEqual(rows[0]['issuer_candidate_cik'], CIK)
        self.assertFalse(rows[0]['identity_verified'])
        self.assertFalse(rows[0]['eligible_for_backtest'])
        self.assertEqual(name_key('THE ODP CORP'), name_key('ODP Corp'))

    def test_short_parent_name_not_matched_as_long_fund(self):
        batch = [{'cusip': CUSIP, 'issuer': 'BLACKROCK ENHANCED LARGE CAP'}]
        rows = propose(batch, {CIK: {'BlackRock Enhanced Large Cap Core Fund Inc.'},
                               '0000000002': {'BlackRock Inc.'}}, {})
        self.assertEqual(rows[0]['issuer_candidate_cik'], CIK)

    def test_ambiguous_names_remain_unselected(self):
        rows = propose([{'cusip': CUSIP, 'issuer': 'BLUE OWL CAPITAL CORP'}],
                       {CIK: {'Blue Owl Capital Corp'}, '0000000002': {'Blue Owl Capital Inc'}}, {})
        self.assertIsNone(rows[0]['issuer_candidate_cik'])

    def test_both_schedule_xml_schemas_and_issuer_not_filer(self):
        for form, upper in [('SCHEDULE 13G', False), ('SCHEDULE 13D/A', True)]:
            result = ownership_identity(body(ownership_xml(upper=upper), form), source(form), CIK, CUSIP)
            self.assertEqual(result['status'], 'matched')
            wrong = ownership_identity(body(ownership_xml(upper=upper), form), source(form), '0000000002', CUSIP)
            self.assertEqual(wrong['status'], 'identity_conflict')

    def test_missing_id_is_parse_issue_not_security_mismatch(self):
        with self.assertRaisesRegex(ValueError, 'missing_or_ambiguous_issuer'):
            ownership_identity(body(ownership_xml(missing=True)), source(), CIK, CUSIP)
        changed = ownership_identity(body(ownership_xml(cusip='00848K200')), source(), CIK, CUSIP)
        self.assertEqual(changed['status'], 'identity_conflict')

    def test_original_and_index_dates_and_acceptance_cutoff(self):
        for changed in [source(filed='2025-11-11'), source(filed='2026-01-01'), source(form='SCHEDULE 13D')]:
            with self.assertRaises(ValueError):
                source_documents(body(ownership_xml()), changed)
        with self.assertRaises(ValueError):
            source_documents(body(ownership_xml(), accepted='20260101000000'), source())

    def test_cover_pairs_fields_by_context_and_ignores_bonds(self):
        bond = fact('Security12bTitle', 'Senior Notes due 2030', 'bond') + fact('TradingSymbol', 'BOND', 'bond') + fact('SecurityExchangeName', 'NYSE', 'bond')
        result = listing_cover(body(cover(bond), '10-Q'), source('10-Q'), CIK)
        self.assertEqual(result['ticker_in_filing'], 'ABC')
        self.assertFalse(result['listing_at_selection_date_confirmed'])

    def test_multiple_common_classes_not_silently_paired(self):
        second = fact('Security12bTitle', 'Common Stock class B', 'b') + fact('TradingSymbol', 'ABC.B', 'b') + fact('SecurityExchangeName', 'Nasdaq', 'b')
        result = listing_cover(body(cover(second), '10-Q'), source('10-Q'), CIK)
        self.assertIsNone(result['ticker_in_filing'])
        self.assertEqual(result['status'], 'needs_class_review')

    def test_separate_primary_and_tagged_original_header(self):
        whole = body(cover(), '10-Q', tagged=True)
        header = whole.split(b'<DOCUMENT>')[0]
        result = listing_cover_parts(cover().encode(), header, source('10-Q'), CIK)
        self.assertEqual(result['ticker_in_filing'], 'ABC')
        with self.assertRaises(ValueError):
            listing_cover_parts(cover().encode(), header, source('10-Q'), '0000000009')

    def test_bond_warrant_and_preferred_removal_do_not_remove_common_stock(self):
        for title, expected in [('Common Stock', 'common_stock_notice'),
                                ('Floating Rate Senior Notes due 2025', 'other_or_unclassified_security_notice'),
                                ('Warrants to purchase Common Stock', 'other_or_unclassified_security_notice'),
                                ('Preferred Stock Purchase Rights', 'other_or_unclassified_security_notice')]:
            xml = f'<XML><notificationOfRemoval><issuer><cik>{CIK}</cik></issuer><descriptionClassSecurity>{title}</descriptionClassSecurity></notificationOfRemoval></XML>'
            result = removal_notice(body(xml, '25-NSE'), source('25-NSE'), CIK)
            self.assertEqual(result['status'], expected)
            self.assertIsNone(result['automatic_delisting_effective_date'])

    def test_reviewed_html_removal_cannot_transfer_to_changed_source_or_issuer(self):
        # 実際の原書類をテストへ埋め込まず、内容の指紋による結び付けを検査する。
        original = body('<html>Common stock (Description of class of securities)</html>', '25')
        original = original.replace(b'</SEC-HEADER>', b'CENTRAL INDEX KEY: 0000000001\n</SEC-HEADER>')
        entry = {'issuer_cik': CIK, 'sha256': hashlib.sha256(original).hexdigest(),
                 'security_class': 'Common stock', 'reviewed_on': '2026-09-10'}
        with patch.dict('flow_probe.identity_sources.HTML_REMOVAL_REVIEWS',
                        {'0000000002-25-000001': entry}, clear=True):
            result = removal_notice(original, source('25'), CIK)
            self.assertEqual(result['status'], 'common_stock_notice')
            self.assertIsNone(result['automatic_delisting_effective_date'])
            for changed, issuer in [(original + b'changed', CIK), (original, '0000000009')]:
                with self.assertRaisesRegex(ValueError, 'reviewed_html_removal_source_changed'):
                    removal_notice(changed, source('25'), issuer)

    def test_unreviewed_html_removal_remains_unknown(self):
        with self.assertRaisesRegex(ValueError, 'missing_or_ambiguous_primary_xml'):
            removal_notice(body('<html>Common stock</html>', '25'), source('25'), CIK)


if __name__ == '__main__':
    unittest.main()
