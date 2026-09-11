"""将来の訂正値・同日公表・期間混在が選定を変えないことを確認する。"""
import unittest
from research.history.quality_trial import financial_snapshot,going_concern_scan,gate_snapshot,TAG_OCF


def facts(tag,rows):return {'facts':{'us-gaap':{tag:{'units':{'USD':rows}}}}}


def row(value,filed='2023-03-01',end='2022-12-31',**kwargs):
    return {'val':value,'filed':filed,'end':end,'accn':filed,'form':'10-K',**kwargs}


class QualityTests(unittest.TestCase):
    def test_future_restatement_and_same_day_excluded(self):
        data=facts(TAG_OCF,[row(3,start='2022-01-01'),row(-50,filed='2023-06-01',start='2022-01-01')])
        self.assertEqual(financial_snapshot(data,'2023-06-01')['operating_cash']['val'],3)
        self.assertEqual(financial_snapshot(data,'2023-06-02')['operating_cash']['val'],-50)

    def test_quarter_is_not_annual_cashflow(self):
        data=facts(TAG_OCF,[row(3,start='2022-10-01')])
        self.assertIsNone(financial_snapshot(data,'2023-03-02')['operating_cash'])

    def test_balance_pair_requires_same_document(self):
        a=facts('AssetsCurrent',[row(100)])
        a['facts']['us-gaap'].update(facts('LiabilitiesCurrent',[row(20,filed='2023-03-02')])['facts']['us-gaap'])
        self.assertIsNone(financial_snapshot(a,'2023-03-03')['working_capital'])

    def test_missing_is_unknown(self):
        self.assertTrue(all(v is None for v in gate_snapshot({},'2023-04-01')['gates'].values()))

    def test_notice_only_after_publication(self):
        issuer={'history_complete':True,'filings':[{'form':'8-K','filingDate':'2023-03-01','items':'3.01,9.01','accessionNumber':'a'}]}
        self.assertTrue(gate_snapshot(issuer,'2023-03-01')['gates']['event_clear'])
        self.assertFalse(gate_snapshot(issuer,'2023-03-02')['gates']['event_clear'])

    def test_negative_going_concern_is_not_positive(self):
        self.assertEqual(going_concern_scan(b'There is no substantial doubt about our ability to continue as a going concern.')['status'],'no_positive_phrase_detected')
        self.assertEqual(going_concern_scan(b'There is substantial doubt about our ability to continue as a going concern.')['status'],'review_required')

    def test_stale_annual_is_unknown(self):
        self.assertIsNone(financial_snapshot(facts(TAG_OCF,[row(3,start='2022-01-01')]),'2024-08-01')['operating_cash'])

if __name__=='__main__':unittest.main()
