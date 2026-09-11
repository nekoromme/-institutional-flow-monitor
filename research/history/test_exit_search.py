import unittest
from research.history.exit_search import exit_reason,simulate_exit


def book(prices):
    return {'A':{d:{'valid':True,'split':{'o':o,'c':c}} for d,o,c in prices}}

class ExitTests(unittest.TestCase):
    def test_trailing_requires_prior_profit(self):
        self.assertIsNone(exit_reason('trail5',90,100,100))
        self.assertEqual(exit_reason('trail5',99,100,105),'trailing')

    def test_stop_uses_next_open_not_threshold_price(self):
        ds=['2023-01-01','2023-01-02','2023-01-03']
        b=book([(ds[0],100,100),(ds[1],100,94),(ds[2],80,90)])
        result,record=simulate_exit([{'symbol':'A','date':ds[0],'x':True}],b,ds,'x',20,'stop5',cost=0)
        self.assertAlmostEqual(result['summary']['two_year_total_return'],-.02)
        self.assertEqual(record['closed'][0]['exit_when'],'open')

    def test_profit_not_sold_at_prior_close(self):
        ds=['2023-01-01','2023-01-02','2023-01-03']
        b=book([(ds[0],100,100),(ds[1],100,112),(ds[2],104,110)])
        result,_=simulate_exit([{'symbol':'A','date':ds[0],'x':True}],b,ds,'x',20,'profit10',cost=0)
        self.assertAlmostEqual(result['summary']['two_year_total_return'],.004)

    def test_fixed_horizon_counts_purchase_day(self):
        ds=['2023-01-01','2023-01-02','2023-01-03','2023-01-04']
        b=book([(d,100,100+i) for i,d in enumerate(ds)])
        _,rec=simulate_exit([{'symbol':'A','date':ds[0],'x':True}],b,ds,'x',2,cost=0)
        self.assertEqual(rec['closed'][0]['exit_date'],ds[2])
        self.assertEqual(rec['closed'][0]['exit_when'],'close')

    def test_exit_day_signal_does_not_immediately_rebuy(self):
        ds=['2023-01-01','2023-01-02','2023-01-03','2023-01-04']
        b=book([(d,100,100) for d in ds])
        rows=[{'symbol':'A','date':d,'x':True} for d in ds]
        _,rec=simulate_exit(rows,b,ds,'x',1,cost=0)
        self.assertEqual([t['date'] for t in rec['fills'] if t['event']=='buy'],[ds[1],ds[3]])
