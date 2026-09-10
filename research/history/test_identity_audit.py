"""別会社・未来の提出・本文中の偶然の番号一致を採用しないための検査。"""
import unittest
import gzip
import hashlib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from research.history.identity_audit import legacy_ownership, official_names, download
from research.history.supplement import requested_symbols

SOURCE={'filename':'edgar/data/123/0000000999-22-000001.txt','form':'SC 13G/A','filed':'2022-11-10'}

def filing(text,subject='0000000123',accepted='20221110160000'):
    return (f'<SEC-HEADER>\n<ACCEPTANCE-DATETIME>{accepted}\n'
        'ACCESSION NUMBER: 0000000999-22-000001\n'
        'CONFORMED SUBMISSION TYPE: SC 13G/A\nFILED AS OF DATE: 20221110\n'
        f'SUBJECT COMPANY:\n COMPANY DATA:\n  CENTRAL INDEX KEY: {subject}\n'
        'FILED BY:\n COMPANY DATA:\n  CENTRAL INDEX KEY: 0000000999\n'
        '</SEC-HEADER><DOCUMENT>\n<TYPE>SC 13G/A\n<TEXT>'+text+'</TEXT></DOCUMENT>').encode()

class AuditChecks(unittest.TestCase):
    def test_resuming_small_scope_keeps_other_already_saved_source_records(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);out=root/'diagnostics/history/audit';out.mkdir(parents=True)
            raw=b'cached original fixture';p=root/'cached.gz';p.write_bytes(gzip.compress(raw))
            first={'filename':'edgar/data/1/first.txt'};other={'filename':'edgar/data/2/other.txt'}
            prior=[{'source':s,'uses':[],'status':'downloaded','path':'cached.gz',
                    'sha256':hashlib.sha256(raw).hexdigest()} for s in (first,other)]
            (out/'downloads.json').write_text(json.dumps({'files':prior}))
            proposals=[{'year':2023,'rows':[{'ordinal':1,'selected_sources':{'listing_cover':first}}]}]
            with redirect_stdout(io.StringIO()):result=download(root,proposals,limit=1)
            self.assertEqual(len(result['files']),2)
            self.assertEqual(len(json.loads((out/'downloads.json').read_text())['files']),2)

    def test_unverified_or_duplicate_security_cannot_enter_price_request(self):
        valid={'cusip_issuer_verified':True,'price_collection_candidate':True,'ticker_in_reviewed_filing':'ABC'}
        for rows in [[dict(valid,cusip_issuer_verified=False)],[valid,valid]]:
            with self.assertRaises(ValueError):
                requested_symbols({'years':{'2023':{'additional_candidates':rows}}},2023)
    def test_issuer_not_reporting_person_and_explicit_cusip_field(self):
        result=legacy_ownership(filing('<p>123456 78 9</p><p>(CUSIP Number)</p>'),SOURCE,'0000000123','123456789','2023-01-01')
        self.assertEqual(result['status'],'matched')
        wrong=legacy_ownership(filing('123456789 (CUSIP Number)'),SOURCE,'0000000999','123456789','2023-01-01')
        self.assertEqual(wrong['status'],'subject_company_mismatch')

    def test_number_without_cover_label_and_conflicting_fields_remain_pending(self):
        for text in ['The account number is 123456789.',
                     '123456789 (CUSIP Number) 987654321 (CUSIP Number)']:
            result=legacy_ownership(filing(text),SOURCE,'0000000123','123456789','2023-01-01')
            self.assertEqual(result['status'],'cusip_cover_requires_review')

    def test_old_label_first_and_invisible_spacing_without_row_number_false_match(self):
        for text in ['CUSIP #123456789', 'CUSIP No.: 123456789',
                     '123456789\u200b (CUSIP Number)',
                     'Type of Reporting Person CO 2 CUSIP No. 123456789']:
            result=legacy_ownership(filing(text),SOURCE,'0000000123','123456789','2023-01-01')
            self.assertEqual(result['status'],'matched',text)

    def test_next_year_acceptance_cannot_enter_previous_year(self):
        with self.assertRaises(ValueError):
            legacy_ownership(filing('123456789 (CUSIP Number)',accepted='20230101000000'),SOURCE,'0000000123','123456789','2023-01-01')

    def test_official_name_uses_number_and_handles_internal_spacing(self):
        text='Year: 2022 Qtr: 3\nRun Date: 10/05/2022\n123456 78 9 * REAL  COMPANY INC    COM\n222222 22 2   OLD INC    COM DELETED\nTotal Count: 2'
        self.assertEqual(official_names(text,2022),{'123456789':'REAL COMPANY INC'})

if __name__=='__main__':unittest.main()
