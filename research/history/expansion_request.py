"""固定済みの照合結果から追加取得を決める。将来の利益は入力にしない。"""
from collections import Counter
import json
from .collect import ROOT,write_json
from flow_probe.bulk13f import digest


def build_request(reviews,previous):
    output={'purpose':'expand_fixed_historical_candidates_without_return_selection',
            'returns_computed':False,'overwrites_previous_market_data':False,'years':{}}
    for review in reviews:
        year=review['summary']['year'];rows=review['rows']
        # 一部の行を成績を見て抜き取った表を渡さないよう、固定枠を検査する。
        if sorted(r['ordinal'] for r in rows)!=list(range(1,201)):
            raise ValueError('complete_fixed_200_candidate_frame_required')
        if len({r['cusip'] for r in rows})!=200:
            raise ValueError('duplicate_security_in_fixed_frame')
        eligible=[r for r in rows if r['cusip_issuer_verified'] and r['price_collection_candidate']]
        counts=Counter(r['ticker_in_reviewed_filing'] for r in eligible)
        known={r['cusip'] for r in previous['years'][str(year)]['additional_candidates']}
        added=[];held=[];already=[]
        for r in eligible:
            if counts[r['ticker_in_reviewed_filing']]>1:
                held.append({'cusip':r['cusip'],'ticker':r['ticker_in_reviewed_filing'],'reason':'multiple_securities_share_ticker'})
            elif r['cusip'] in known:already.append(r['cusip'])
            else:
                added.append({k:r[k] for k in ('ordinal','cusip','issuer_candidate_cik','cusip_issuer_verified',
                    'price_collection_candidate','ticker_in_reviewed_filing','backtest_eligible')})
        output['years'][str(year)]={'additional_candidates':added,'already_collected_same_security':already,
                                   'held_identity_collisions':held,'frame_count':len(rows)}
    if set(output['years'])!={'2023','2024','2025'}:raise ValueError('three_fixed_years_required')
    return output


def main():
    paths=[ROOT/f'diagnostics/history/audit/review-{y}.json' for y in (2023,2024,2025)]
    previous=ROOT/'research/history/price-request.json'
    request=build_request([json.loads(p.read_text()) for p in paths],json.loads(previous.read_text()))
    request['previous_request_sha256']=digest(previous)
    for p in paths:
        y=json.loads(p.read_text())['summary']['year'];request['years'][str(y)]['review_sha256']=digest(p)
    write_json(ROOT/'research/history/expansion-request.json',request)
    print(json.dumps({y:len(r['additional_candidates']) for y,r in request['years'].items()}))

if __name__=='__main__':main()
