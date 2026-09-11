"""価格変化の符号でなく、約定直前の気配に対する位置で主導方向を推定。

真の注文方向ラベルはない。売買双方が必ず存在するので『買い越し』とは
呼ばない。売り気配を取りにいった買い/買い気配へぶつけた売りの代理指標。
"""
import json,os
from bisect import bisect_left
from collections import Counter
from datetime import datetime,timedelta
from flow_probe.alpaca import fetch_pages,NY,timestamp_ns,numeric
from flow_probe.http_client import SafeHttp
from flow_probe.bulk13f import digest
from .collect import ROOT,write_json,encrypt_bytes,decrypt_bytes


def classify(trades,quotes,max_age=1,lag_ms=0):
    # 同一時刻の気配は前後関係を保証できないため使わない。
    q=sorted(quotes,key=lambda x:timestamp_ns(x['t']));stamps=[timestamp_ns(x['t']) for x in q]
    volume=Counter();count=Counter();mids=[];conditions=Counter()
    for t in trades:
        size=t.get('s');price=t.get('p');c=t.get('c',[]);conditions.update(c)
        if not numeric(size) or size<=0 or not numeric(price) or price<=0:count['invalid_trade']+=1;continue
        volume['total']+=size;count['total']+=1
        # 通常/自動執行/端株の限定集合。場外時間や遅延等の混入を安易に分類しない。
        if not c or not set(c)<={'@','F','I'}:volume['unsupported_condition']+=size;continue
        ns=timestamp_ns(t['t']);i=bisect_left(stamps,ns-int(lag_ms*1e6))-1
        if i<0:volume['no_prior_quote']+=size;continue
        b=q[i];age=(ns-stamps[i])/1e9;bid,ask=b.get('bp'),b.get('ap')
        if age>max_age:volume['stale_quote']+=size;continue
        if not all(numeric(v) and v>0 for v in (bid,ask,b.get('bs'),b.get('as'))) or bid>=ask:volume['invalid_or_locked_quote']+=size;continue
        mid=(bid+ask)/2;mids.append(mid)
        # 気配の外で約定したものも時刻ずれ等が疑われるので不明にする。
        if price<bid-1e-8 or price>ask+1e-8:volume['outside_quote']+=size;continue
        if abs(price-ask)<1e-8:side='buy'
        elif abs(price-bid)<1e-8:side='sell'
        else:side='inside_spread'
        volume[side]+=size;count[side]+=1
        # 中値基準は補助。中値そのものは方向を割り当てない。
        if price>mid+1e-8:volume['mid_buy']+=size
        elif price<mid-1e-8:volume['mid_sell']+=size
        else:volume['mid_unknown']+=size
    classified=volume['buy']+volume['sell'];midclassified=volume['mid_buy']+volume['mid_sell']
    return {'max_quote_age_seconds':max_age,'quote_lag_ms':lag_ms,'trade_count':dict(count),'share_volume':dict(volume),'conditions':dict(conditions),
        'edge_classification_share_fraction':classified/volume['total'] if volume['total'] else None,
        'edge_buy_fraction':volume['buy']/classified if classified else None,
        'midpoint_buy_fraction':volume['mid_buy']/midclassified if midclassified else None,
        'matched_quote_midpoint_change':mids[-1]/mids[0]-1 if len(mids)>1 else None,
        'direction_truth_verified':False}


def run(root,secret,key):
    prior=json.loads((root/'diagnostics/history/quiet/results.json').read_text())
    out={'conditional_trigger':prior['direction_probe_required'],'full_day_coverage':False,'profit_predictiveness_tested':False,'samples':[]}
    if not prior['direction_probe_required']:
        write_json(root/'diagnostics/history/quiet/direction.json',out);return out
    client=SafeHttp(key,secret,timeout=25,max_requests=180,max_seconds=600);private=[]
    for sample in prior['direction_samples']:
        for hour in (10,14):
            start=datetime.fromisoformat(sample['date']+f'T{hour}:00:00').replace(tzinfo=NY);end=start+timedelta(minutes=30)
            records={};meta={}
            for kind in ('trades','quotes'):
                # 気配だけ5秒先に取得開始して直前気配の不足を減らす。
                rows,m=fetch_pages(client,kind,(sample['symbol'],),start-timedelta(seconds=5) if kind=='quotes' else start,end,feed='sip',limit=10000,max_pages=10)
                records[kind]=rows[sample['symbol']];meta[kind]=m
            complete=all(m['complete'] for m in meta.values())
            result={**sample,'hour_NY':hour,'complete':complete,'metadata':meta,
                    'classifications':[classify(records['trades'],records['quotes'],age,lag) for age,lag in ((1,0),(5,0),(1,1))] if complete else []}
            out['samples'].append(result);private.append({**sample,'hour_NY':hour,'records':records,'metadata':meta})
    raw=json.dumps(private,sort_keys=True,allow_nan=False).encode();enc=encrypt_bytes(raw,secret);assert decrypt_bytes(enc,secret)==raw
    path=root/'data/history/encrypted/direction-probe.enc';path.write_bytes(enc);out['encrypted_sha256']=digest(path);out['http']=client.metrics()
    write_json(root/'diagnostics/history/quiet/direction.json',out);return out
if __name__=='__main__':run(ROOT,os.environ.get('ALPACA_SECRET_KEY',''),os.environ.get('ALPACA_API_KEY',''))
