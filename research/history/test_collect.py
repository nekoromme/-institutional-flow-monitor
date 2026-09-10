"""原データの漏えい・未来混入・欠けの誤解につながる箇所を点検する。"""
import csv
import io
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile
from cryptography.fernet import InvalidToken
from research.history.collect import (encrypt_bytes, decrypt_bytes, bar_quality, stable_batch,
                                     build_reported_frame, fetch_file)


class StorageChecks(unittest.TestCase):
    secret = 'test-only-secret-not-a-real-api-key'

    def test_encryption_roundtrip_does_not_expose_original(self):
        raw = b'{"prices":[12345.67],"private":"test fixture"}'
        encrypted = encrypt_bytes(raw,self.secret)
        self.assertNotIn(raw,encrypted)
        self.assertEqual(decrypt_bytes(encrypted,self.secret),raw)
        self.assertNotEqual(encrypted,encrypt_bytes(raw,self.secret))

    def test_wrong_key_and_tampering_are_rejected(self):
        encrypted = encrypt_bytes(b'fixture',self.secret)
        with self.assertRaises(InvalidToken):decrypt_bytes(encrypted,'different-test-secret-at-least-16')
        damaged=bytearray(encrypted);damaged[-10]^=1
        with self.assertRaises(InvalidToken):decrypt_bytes(bytes(damaged),self.secret)
        with self.assertRaises(ValueError):encrypt_bytes(b'fixture','')

    def test_missing_and_zero_are_separate(self):
        row={'t':'2023-01-03T05:00:00Z','o':2,'h':3,'l':1,'c':2,'v':0}
        q=bar_quality({'A':[row],'B':[]},['2023-01-03','2023-01-04'])
        self.assertEqual(q['A']['zero_volume_rows'],1)
        self.assertEqual(q['A']['missing_sessions'],1)
        self.assertEqual(q['B']['missing_sessions'],2)
        self.assertEqual(q['B']['records'],0)

    def test_invalid_duplicate_and_non_session_rows_are_reported(self):
        row={'t':'2023-01-03T05:00:00Z','o':2,'h':1,'l':3,'c':2,'v':-1}
        q=bar_quality({'A':[row,row]},['2023-01-04'])['A']
        self.assertEqual(q['duplicate_dates'],1)
        self.assertEqual(q['invalid_rows'],2)
        self.assertEqual(q['non_session_dates'],['2023-01-03'])

    def test_batch_does_not_depend_on_outcomes(self):
        frame=[{'cusip':f'{i:09}'} for i in range(300)]
        actual=[r['cusip'] for r in stable_batch(frame)]
        changed=[dict(r,future_profit=999) for r in reversed(frame)]
        self.assertEqual(actual,[r['cusip'] for r in stable_batch(changed)])
        self.assertEqual(len(actual),200)

    def test_only_selected_filings_enter_the_frame(self):
        states={'manager':{'issues':[], 'filings':[{'accession':'known','cik':'123','filed':'2022-11-14'}]}}
        def tsv(headers,rows):
            out=io.StringIO();w=csv.writer(out,delimiter='\t');w.writerow(headers);w.writerows(rows);return out.getvalue()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'fixture.zip'
            with ZipFile(path,'w') as z:
                z.writestr('SUBMISSION.tsv',tsv(['ACCESSION_NUMBER'],[['known'],['future']]))
                z.writestr('INFOTABLE.tsv',tsv(['ACCESSION_NUMBER','CUSIP','NAMEOFISSUER','TITLEOFCLASS','SSHPRNAMT','SSHPRNAMTTYPE','PUTCALL'],[
                    ['known','123456789','Old company','COM','10','SH',''],
                    ['future','987654321','Future company','COM','10','SH',''],
                    ['known','111111111','Option','COM','10','SH','CALL']]))
            frame,_=build_reported_frame([path],states)
        self.assertEqual([r['cusip'] for r in frame],['123456789'])
        self.assertIsNone(frame[0]['ticker_at_selection'])
        self.assertFalse(frame[0]['low_ownership_verified'])

    def test_resume_checks_hash_before_reusing_data(self):
        class Client:
            calls=0
            def read(self,*args,**kwargs):self.calls+=1;return b'fixture'
        c=Client()
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'archive.zip'
            fetch_file(c,p,'https://www.sec.gov/test')
            fetch_file(c,p,'https://www.sec.gov/test')
            self.assertEqual(c.calls,1)
            p.write_bytes(b'changed')
            with self.assertRaises(ValueError):fetch_file(c,p,'https://www.sec.gov/test')

if __name__=='__main__':unittest.main()
