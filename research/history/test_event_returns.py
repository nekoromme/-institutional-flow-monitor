"""併合で利益を捏造せず、買収・欠損・未約定を混同しない検査。"""
import unittest
from pathlib import Path
from research.history.event_returns import trade_value,shares_between,prepare_prices
from research.history.adjustment_scan import load_development

DAYS=['2024-04-01','2024-04-02','2024-04-03','2024-04-04','2024-04-05','2024-04-08']

def bar(price):return {'o':price,'h':price,'l':price,'c':price,'v':100}
def book(prices):return {'A':{d:{'raw':bar(p),'valid':True} for d,p in zip(DAYS,prices)}}
def attempt(entry=None,end=None):return {'symbol':'A','entry_date':entry or DAYS[0],'exit_date':end or DAYS[-1]}
def split(day,ratio):return {'symbol':'A','kind':'split','effective_date':day,'new_shares_per_old_share':ratio,'source_sha256':'fixture'}
def cash():return {'symbol':'A','kind':'cash_merger_trading_end','first_non_trading_date':DAYS[-1],'cash_per_share_usd':1.1,'source_sha256':'fixture'}

class EventChecks(unittest.TestCase):
    def test_reverse_split_is_not_a_tenfold_profit(self):
        r=trade_value(attempt(),book([1,1,10,10,10,10]),DAYS,[split(DAYS[2],.1)])
        self.assertAlmostEqual(r['price_return'],0)
        self.assertLess(r['net_return'],0)

    def test_split_on_entry_day_is_already_in_entry_price(self):
        r=trade_value(attempt(DAYS[2]),book([1,1,10,10,10,10]),DAYS,[split(DAYS[2],.1)])
        self.assertAlmostEqual(r['price_return'],0)

    def test_cash_entitlement_survives_absence_of_post_merger_prices(self):
        r=trade_value(attempt(),book([1,1,1,1,1]),DAYS,[cash()])
        self.assertEqual(r['status'],'priced');self.assertAlmostEqual(r['price_return'],.1)
        self.assertEqual(r['valuation_kind'],'cash_entitlement');self.assertIsNone(r['cash_payment_date'])

    def test_cash_after_split_uses_changed_share_count(self):
        c=cash();c['cash_per_share_usd']=11
        r=trade_value(attempt(),book([1,1,10,10,10]),DAYS,[split(DAYS[2],.1),c])
        self.assertAlmostEqual(r['price_return'],.1)

    def test_missing_prices_are_not_automatically_cash_payments(self):
        r=trade_value(attempt(),book([1,1,1,1,1]),DAYS,[])
        self.assertEqual(r['status'],'unresolved')

    def test_no_purchase_after_trading_ends(self):
        r=trade_value(attempt(DAYS[-1]),book([1,1,1,1,1]),DAYS,[cash()])
        self.assertEqual(r['status'],'not_executable')

    def test_future_split_not_in_prior_feature_units(self):
        self.assertEqual(shares_between([split('2026-01-01',.1)],'A',DAYS[0],DAYS[-1]),1)

    def test_future_split_validates_provider_units_without_changing_signal(self):
        from datetime import date,timedelta
        from research.history.development_validation import prepare_signals
        days=[(date(2023,1,1)+timedelta(days=i)).isoformat() for i in range(70)]
        raw=[{**bar(1),'t':d+'T05:00:00Z','v':200 if i==69 else 100} for i,d in enumerate(days)]
        sessions=[{'date':d,'open':'09:30','close':'16:00'} for d in days]
        original={'retrieved_at_utc':'2026-09-10','sessions':sessions,'prices':{'raw':{'A':raw},'split':{'A':raw}}}
        adjusted=[{**r,**{k:r[k]*10 for k in ('o','h','l','c')},'v':r['v']/10} for r in raw]
        revised={**original,'prices':{'raw':{'A':raw},'split':{'A':adjusted}}}
        e=[split('2026-01-01',.1)]
        a,qa=prepare_prices(original,[]);b,qb=prepare_prices(revised,e)
        self.assertEqual(qa,qb)
        self.assertEqual(prepare_signals(original,a,[],2023),prepare_signals(revised,b,e,2023))
        self.assertTrue(prepare_signals(revised,b,e,2023)[0][-1]['abnormal'])

    def test_reserved_year_rejected_before_file_access(self):
        with self.assertRaisesRegex(ValueError,'reserved_year'):
            load_development(Path('/nonexistent'),2025,'fixture')

if __name__=='__main__':unittest.main()
