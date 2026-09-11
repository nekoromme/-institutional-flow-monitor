"""未来の値を使わず、調整済み株数の単位変更で判定が変わらないこと。"""
import unittest
from copy import deepcopy
from datetime import date,timedelta
from research.history.practical_validation import prepare,add_variants
from research.history.development_validation import prepare_signals

class PracticalChecks(unittest.TestCase):
    def fixture(self):
        days=[(date(2023,1,1)+timedelta(days=i)).isoformat() for i in range(100)]
        bars=[{'t':d+'T05:00:00Z','o':10,'h':11,'l':9,'c':10+i*.01,'v':200000 if i<70 else 2000000} for i,d in enumerate(days)]
        # high must cover final close.
        for b in bars:b['h']=12
        return {'sessions':[{'date':d,'open':'09:30','close':'16:00'} for d in days], 'prices':{a:{'TEST':deepcopy(bars)} for a in ('raw','split','split,dividend')}}
    def signals(self,p):
        b,_=prepare(p);r,d=prepare_signals(p,b,[],2023);add_variants(r,b,p,d);return r
    def test_future_observations_do_not_change_earlier_signals(self):
        p=self.fixture();q=deepcopy(p)
        for a in q['prices']:
            for b in q['prices'][a]['TEST'][85:]:b.update(o=100,h=110,l=90,c=100,v=99999999)
        cutoff=p['sessions'][84]['date']
        self.assertEqual([r for r in self.signals(p) if r['date']<=cutoff],[r for r in self.signals(q) if r['date']<=cutoff])
    def test_adjustment_unit_change_preserves_decisions(self):
        p=self.fixture();q=deepcopy(p)
        for a in ('split','split,dividend'):
            for b in q['prices'][a]['TEST']:
                for k in ('o','h','l','c'):b[k]*=2
                b['v']/=2
        fields=('abnormal','repeated','repeated_up','price_up_only','repeated_up_capped','repeated_up_liquid')
        self.assertEqual([[r[f] for f in fields] for r in self.signals(p)],[[r[f] for f in fields] for r in self.signals(q)])
