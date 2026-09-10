"""比較の標本数・不明扱い・条件固定に関わる検査。"""
import unittest
from flow_probe.batch_common import observed_change
from flow_probe.batch_market import quarter_signal
from flow_probe.batch_compare import contrast, compare
from flow_probe.class_full import verify_fragment
from flow_probe.filing_review import object_hash
from test_class_expansion import fixture


def holding(positions):
    return {'ledger':[{'cik':c,'shares':s} for c,s in positions.items()],
            'reported_share_sum_before_overlap_resolution':sum(positions.values())}


class BatchTests(unittest.TestCase):
    def test_absent_quarter_not_zero_or_decline(self):
        r=observed_change(holding({'a':100}),holding({}))
        self.assertIsNone(r['observed_reported_increase'])
        self.assertIsNone(r['observed_reported_share_change'])
        self.assertIsNone(r['actual_institutional_net_buy'])

    def test_matched_manager_change_can_oppose_total(self):
        r=observed_change(holding({'a':100,'b':200}),holding({'a':110,'c':20}))
        self.assertFalse(r['observed_reported_increase'])
        self.assertTrue(r['matched_manager_increase'])
        self.assertEqual(r['previous_only_managers'],1)

    def test_unknown_day_not_negative_and_many_days_only_one_vote(self):
        rows=[{'abnormal':True} for _ in range(61)]
        self.assertIs(quarter_signal(rows,'abnormal',61),True)
        rows[0]['abnormal']=None
        self.assertIsNone(quarter_signal(rows,'abnormal',61))
        self.assertIsNone(quarter_signal(rows[1:],'abnormal',61))

    def test_zero_controls_does_not_produce_effect(self):
        r=contrast([{'symbol':'a','s':True,'o':True}], 's','o')
        self.assertEqual(r['signal']['n'],1)
        self.assertEqual(r['no_signal']['n'],0)
        self.assertIsNone(r['difference_percentage_points'])

    def test_unknown_outcomes_do_not_enter_denominator(self):
        rows=[{'symbol':'a','s':True,'o':None},{'symbol':'b','s':True,'o':False},
              {'symbol':'c','s':False,'o':True},{'symbol':'d','s':None,'o':True}]
        r=contrast(rows,'s','o');self.assertEqual(r['signal']['n'],1)
        self.assertEqual(r['no_signal']['n'],1);self.assertEqual(r['difference_percentage_points'],-100)

    def test_protocol_mismatch_blocks_join(self):
        with self.assertRaisesRegex(ValueError,'protocol_mismatch'):
            compare({}, {'protocol_sha256':'changed'},{'protocol_sha256':'changed'})

    def test_fragment_review_requires_same_visible_text_and_numeric_context(self):
        row,identity,text,review=fixture(True)
        review['reviewed_fragment']='Common stock issued and outstanding 100'
        review['fragment_sha256']=object_hash(review['reviewed_fragment'])
        self.assertTrue(verify_fragment(row,identity,text,review)['security_class_verified'])
        with self.assertRaisesRegex(ValueError,'fragment_missing'):
            verify_fragment(row,identity,text.replace('<td>100</td>','<td>200</td>'),review)
        with self.assertRaisesRegex(ValueError,'numeric_fact_changed'):
            verify_fragment(row,identity,text.replace('2025-09-30','2025-12-31'),review)


if __name__=='__main__':unittest.main()
