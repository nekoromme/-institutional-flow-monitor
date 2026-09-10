"""日足価格のずれを、事前に選んだ4事例の約定・気配で調べる。

参考時刻の買い気配・売り気配を観測するだけで、注文は出さない。
気配数量で指定金額が全部約定するとは認定しない。
"""
from collections import Counter
from datetime import datetime, timedelta
import hashlib
import json
import math

from .alpaca import NY, fetch_pages, iso, numeric, timestamp_ns
from .returns_core import by_date, valid_bar

CASES=(('TGTX','2026-01-12','entry'),('CNXC','2026-01-15','entry'),
       ('SMSI','2026-02-20','exit'),('KAPA','2026-03-13','exit'))


def source_hash(rows):
    return hashlib.sha256(json.dumps(rows,sort_keys=True,allow_nan=False).encode()).hexdigest()


def quote_reference(rows, moment, daily_price, leg, *, complete):
    """指定時刻より後を見ない。直近60秒の正常な気配だけを参考にする。"""
    cutoff=timestamp_ns(iso(moment));usable=[]
    for r in rows:
        if not all(numeric(r.get(k)) and r[k]>0 for k in ('bp','ap','bs','as')):continue
        if r['ap']<r['bp']:continue  # 売値と買値が逆転した気配は今回使わない。
        stamp=timestamp_ns(r['t'])
        if 0<=cutoff-stamp<=60*1_000_000_000:usable.append((stamp,r))
    if not complete or not usable or not numeric(daily_price) or daily_price<=0:
        return {'status':'unknown','reason':'incomplete_or_no_fresh_valid_quote'}
    stamp,r=max(usable,key=lambda x:x[0]);mid=(r['ap']+r['bp'])/2
    side=r['ap'] if leg=='entry' else r['bp']
    return {'status':'observed_quote_reference','quote_age_seconds':(cutoff-stamp)/1e9,
            'full_spread_fraction_of_mid':(r['ap']-r['bp'])/mid,
            'one_side_half_spread_fraction_of_mid':(r['ap']-r['bp'])/(2*mid),
            'quote_side_relative_to_daily_reference':side/daily_price-1,
            'displayed_sizes_positive':True,'requested_order_size_filled':None,
            'limitation':'point_in_time_quote_not_fill; spread_and_price_movement_not_interchangeable'}


def inspect_execution(client,books):
    reports=[];private=[]
    for symbol,day,leg in CASES:
        # 購入は開始1分後、売却は終了1分前の気配を固定して見る。
        # 日足バックテストの買値を、この標本で都合よく差し替えない。
        moment=datetime.fromisoformat(day+('T09:31:00' if leg=='entry' else 'T15:59:00')).replace(tzinfo=NY)
        start=moment-timedelta(minutes=1) if leg=='entry' else moment
        end=start+timedelta(minutes=2)-timedelta(microseconds=1)
        trade,tm=fetch_pages(client,'trades',(symbol,),start,end,feed='sip',limit=10000,max_pages=3)
        bars,bm=fetch_pages(client,'bars',(symbol,),start,end,feed='sip',timeframe='1Min',limit=1000,max_pages=1)
        quotes,qm=fetch_pages(client,'quotes',(symbol,),moment-timedelta(minutes=1),moment,
                             feed='sip',limit=10000,max_pages=3)
        daily=books.get(symbol,{}).get(day,{}).get('raw',{})
        field='o' if leg=='entry' else 'c';reference=daily.get(field)
        matches=[r for r in trade[symbol] if numeric(reference) and numeric(r.get('p'))
                 and math.isclose(r['p'],reference,rel_tol=1e-6,abs_tol=0.0001)]
        condition_counts=Counter(c for r in trade[symbol] for c in r.get('c',[]))
        result={'symbol':symbol,'date':day,'leg':leg,'quote_reference_time':iso(moment),
                'probes':[tm,bm,qm],'trade_rows':len(trade[symbol]),'minute_rows':len(bars[symbol]),
                'trade_condition_counts':dict(condition_counts),
                'daily_price_matching_trades':len(matches),
                'first_daily_price_matching_trade_time':matches[0]['t'] if matches else None,
                'matching_trade_conditions':dict(Counter(c for r in matches for c in r.get('c',[]))),
                'quote_reference':quote_reference(quotes[symbol],moment,reference,leg,complete=qm['complete']),
                'source_hashes':{k:source_hash(v[symbol]) for k,v in [('trades',trade),('bars',bars),('quotes',quotes)]}}
        reports.append(result);private.append({'symbol':symbol,'day':day,'leg':leg,
                                              'trades':trade,'bars':bars,'quotes':quotes})
    return {'cases':reports,'actual_execution_verified':False,
            'selection':'four_previously_identified_reference_anomalies_not_representative_cost_sample',
            'size_limitation':'positive_displayed_size_checked; units_and_requested_notional_capacity_not_validated'},private


def dividend_shapes(data):
    """配当補正の共通倍率という旧仮定を点検する。現金配当は推定しない。"""
    output=[]
    for symbol,rows in data['split'].items():
        adjusted=by_date(data['split,dividend'].get(symbol,[]));counts=Counter()
        for day,a in by_date(rows).items():
            b=adjusted.get(day)
            if not valid_bar(a) or not valid_bar(b):counts['invalid_or_missing']+=1;continue
            factor=b['c']/a['c'];offset=b['c']-a['c']
            ratio=all(math.isclose(b[k],a[k]*factor,rel_tol=1e-6,abs_tol=0.0002) for k in ('o','h','l','c'))
            additive=all(math.isclose(b[k]-a[k],offset,rel_tol=1e-6,abs_tol=0.0002) for k in ('o','h','l','c'))
            counts['uniform_ratio' if ratio else 'uniform_offset_only' if additive else 'neither']+=1
        output.append({'symbol':symbol,**dict(counts)})
    return {'rows':output,'used_to_change_primary_returns':False,
            'interpretation':'data_shape_audit_not_cash_dividend_or_total_return_validation'}
