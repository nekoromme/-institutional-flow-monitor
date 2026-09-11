import unittest
from research.history.annual_continuous import annual_summaries, merge_books

class ContinuousTests(unittest.TestCase):
    def test_year_boundary_carries_equity_and_positions(self):
        daily=[]
        for d,e,p in [('2023-12-29',1.1,1),('2024-01-02',1,1),('2024-12-31',1.05,0)]:
            daily.append({'date':d,'equity':e,'cash':0 if p else e,
                          'stock_value':e if p else 0,'positions':p,'invested_fraction':float(p)})
        a,b=annual_summaries({'daily':daily,'fills':[]})
        self.assertAlmostEqual(a['annual_return'],.1)
        self.assertAlmostEqual(b['annual_return'],1.05/1.1-1)
        self.assertEqual(b['incoming_positions'],1)
        self.assertAlmostEqual((1+a['annual_return'])*(1+b['annual_return']),1.05)

    def test_overlap_unit_change_is_not_silently_merged(self):
        b={'valid':True,'split':dict(o=100,h=100,l=100,c=100,v=100)}
        dest={'A':{'day':b}}
        self.assertEqual(merge_books(dest,{'A':{'day':b}}),1)
        changed={'valid':True,'split':dict(o=50,h=50,l=50,c=50,v=200)}
        with self.assertRaisesRegex(ValueError,'basis_mismatch'):
            merge_books(dest,{'A':{'day':changed}})
