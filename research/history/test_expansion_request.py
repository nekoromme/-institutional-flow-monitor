"""収集済みとの区別と、同じコードへの複数証券の混入を点検。"""
import unittest
from research.history.expansion_request import build_request

def fixtures():
    reviews=[];previous={'years':{}}
    for year in (2023,2024,2025):
        rows=[{'ordinal':i,'cusip':f'{i:09}','cusip_issuer_verified':True,'price_collection_candidate':True,
               'ticker_in_reviewed_filing':f'A{i}','issuer_candidate_cik':f'{i:010}','backtest_eligible':False} for i in range(1,201)]
        reviews.append({'summary':{'year':year},'rows':rows})
        previous['years'][str(year)]={'additional_candidates':[rows[0]]}
    return reviews,previous

class ExpansionChecks(unittest.TestCase):
    def test_previously_collected_security_is_not_requested_again(self):
        r,p=fixtures();out=build_request(r,p)
        self.assertEqual(len(out['years']['2023']['additional_candidates']),199)
        self.assertEqual(out['years']['2023']['already_collected_same_security'],['000000001'])

    def test_colliding_tickers_are_both_held(self):
        r,p=fixtures();r[0]['rows'][1]['ticker_in_reviewed_filing']='A3'
        out=build_request(r,p)['years']['2023']
        self.assertEqual(len(out['held_identity_collisions']),2)
        self.assertFalse(any(x['ticker_in_reviewed_filing']=='A3' for x in out['additional_candidates']))

    def test_partial_frame_and_duplicate_security_are_rejected(self):
        for mode in ('missing','duplicate'):
            r,p=fixtures()
            if mode=='missing':r[0]['rows'].pop()
            else:r[0]['rows'][1]['cusip']=r[0]['rows'][0]['cusip']
            with self.assertRaises(ValueError):build_request(r,p)

if __name__=='__main__':unittest.main()
