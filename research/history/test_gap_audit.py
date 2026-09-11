"""コード切替で別期間を混ぜず、出来高ゼロの標本を都合で選ばない。"""
import unittest
from pathlib import Path
from research.history.gap_audit import rename_merge,zero_samples,load_expanded

def bar(day,volume=0):return {'t':day+'T05:00:00Z','v':volume,'n':0}

class GapChecks(unittest.TestCase):
    def test_rename_keeps_old_before_and_new_after_boundary(self):
        a=[bar('2024-01-16',20)];b=[bar('2024-01-15',30),bar('2024-01-17',40)]
        out=rename_merge(a,b,'2024-01-17')
        self.assertEqual([r['v'] for r in out],[20,40])
        self.assertEqual([r['source_symbol'] for r in out],['LAZY','GORV'])
    def test_existing_old_rows_after_boundary_are_not_silently_overwritten(self):
        with self.assertRaises(ValueError):rename_merge([bar('2024-01-17')],[bar('2024-01-17')],'2024-01-17')
    def test_sampling_first_last_and_singleton(self):
        p={'year':2023,'prices':{'raw':{'A':[bar('2023-01-03'),bar('2023-01-04'),bar('2023-01-05')],'B':[bar('2023-01-03')]}}}
        a,b=zero_samples(p);self.assertEqual(a['sample_days'],['2023-01-03','2023-01-05']);self.assertEqual(b['sample_days'],['2023-01-03'])
    def test_reserved_year_cannot_be_decrypted(self):
        with self.assertRaises(ValueError):load_expanded(Path('/nonexistent'),2025,'fixture')

if __name__=='__main__':unittest.main()
