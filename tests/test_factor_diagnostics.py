"""原因の切り分けが本番モデルの変更や欠損の0埋めにならないことを確認。"""
import copy
import unittest
from test_cjmb_pilot import MarketFixture
from flow_probe.factor_diagnostics import constant_scale, laboratory_score, leave_one_out
from flow_probe.research import prepare_daily
from flow_probe.pilot import score


class FactorDiagnosticTests(unittest.TestCase):
    def test_laboratory_positive_data_agrees_with_existing_model(self):
        f=MarketFixture();prepared,_=prepare_daily({'CJMB':f.rows},f.sessions,{'CJMB':{'status':'comparable_sample'}})
        expected=[{k:r[k] for k in ['date','abnormal','repeated']} for r in score(prepared,[s['date'] for s in f.sessions])]
        self.assertEqual(laboratory_score(f.rows,f.sessions),expected)

    def test_uniform_reciprocal_scale_preserves_judgments(self):
        f=MarketFixture();adjusted=copy.deepcopy(f.rows)
        for r in adjusted:
            for k in ['o','h','l','c']:r[k]*=5
            r['v']/=5
        self.assertEqual(constant_scale(f.rows,adjusted)['status'],'uniform_reciprocal_scale')
        self.assertEqual(laboratory_score(f.rows,f.sessions),laboratory_score(adjusted,f.sessions))
        adjusted[-1]['v']+=100
        self.assertEqual(constant_scale(f.rows,adjusted)['status'],'nonuniform_or_inconsistent_scale')

    def test_zero_assumption_does_not_fill_missing_sessions(self):
        f=MarketFixture(missing=True)
        r=laboratory_score(f.rows,f.sessions,allow_zero=True)
        self.assertIsNone(next(x for x in r if x['date']=='2026-01-05')['abnormal'])
        f=MarketFixture();i=next(i for i,s in enumerate(f.sessions) if s['date']=='2026-01-05');f.rows[i]['v']=0
        a=laboratory_score(f.rows,f.sessions);b=laboratory_score(f.rows,f.sessions,allow_zero=True)
        self.assertGreater(sum(x['abnormal'] is not None for x in b),sum(x['abnormal'] is not None for x in a))
        self.assertFalse(next(x for x in b if x['date']=='2026-01-05')['abnormal'])

    def test_singleton_deletion_cannot_create_empty_group_effect(self):
        result=leave_one_out([{'symbol':'a','s':True,'o':True},{'symbol':'b','s':False,'o':False}])
        self.assertEqual(result['baseline_difference_points'],100)
        self.assertEqual(result['valid_deletions'],0)
        self.assertIsNone(result['minimum_difference_points'])


if __name__=='__main__':unittest.main()
