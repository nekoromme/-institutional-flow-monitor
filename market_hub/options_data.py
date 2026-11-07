"""Alpacaのオプションを読む。参考配信・実配信・現在値・過去を別物として記録。

無料キーでどこまで読めるかは応答で確定する。OPRAが拒否されたときも、
参考配信へ黙って切り替えて『OPRAを取得した』ことにはしない。
"""
import re
from datetime import datetime, timedelta, timezone
from flow_probe.http_client import ProbeError
from .source_access import SourceHttp
from .store import utcnow, timestamp

BASE='https://data.alpaca.markets/v1beta1/options/'


def collect_chain(store, underlying, *, feed, client=None, strike_min=None, strike_max=None):
    if not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}',underlying) or feed not in ('opra','indicative'):
        raise ValueError('invalid_option_request')
    client=client or SourceHttp()
    now=datetime.now(timezone.utc)
    params={'feed':feed,'limit':1000,'expiration_date_gte':now.date().isoformat(),
            'expiration_date_lte':(now.date()+timedelta(days=35)).isoformat()}
    if strike_min is not None:
        params['strike_price_gte']=strike_min
        params['strike_price_lte']=strike_max
    groups={};tokens=set()
    url=BASE+'snapshots/'+underlying
    for _ in range(20):
        payload=client.json(url,params)
        snapshots=payload.get('snapshots')
        if not isinstance(snapshots,dict):
            raise ValueError('unexpected_option_snapshot_schema')
        for symbol,value in snapshots.items():
            if not re.fullmatch(re.escape(underlying)+r'[0-9]?\d{6}[CP]\d{8}',symbol):
                raise ValueError('unexpected_option_contract')
            if symbol in groups:
                raise ValueError('duplicate_option_snapshot')
            if not isinstance(value,dict):
                raise ValueError('invalid_option_snapshot')
            groups[symbol]=value
        token=payload.get('next_page_token')
        if not token:
            break
        if token in tokens:
            raise ValueError('repeated_option_page_token')
        tokens.add(token);params['page_token']=token
    else:
        raise ValueError('option_chain_incomplete')
    if not groups:
        raise ValueError('empty_option_chain')
    metadata={'retrieved_at_utc':utcnow(),'source_url':url,'feed_requested':feed,
              'feed_provenance':'explicit_request; provider_response_not_required_to_echo_feed',
              'coverage':'expiration_next_35_calendar_days; strike_filter_if_provided',
              'strike_filter':[strike_min,strike_max],'snapshot_only':True,
              'indicative_derivatives':feed=='indicative','historical_greeks':False,
              'open_interest_included':False,'complete_for_requested_filters':True}
    identifier=store.put('alpaca_option_snapshot_'+feed,underlying,metadata,groups)
    return identifier,groups


def collect_contracts(store, underlying, *, client=None, strike_min=None, strike_max=None):
    """現在提供されている建玉とその基準日、契約倍率を保存。過去へ遡及しない。"""
    if not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}',underlying):
        raise ValueError('invalid_option_request')
    client=client or SourceHttp()
    now=datetime.now(timezone.utc)
    params={'underlying_symbols':underlying,'status':'active','limit':1000,
            'expiration_date_gte':now.date().isoformat(),
            'expiration_date_lte':(now.date()+timedelta(days=35)).isoformat()}
    if strike_min is not None:
        params.update(strike_price_gte=strike_min,strike_price_lte=strike_max)
    url='https://paper-api.alpaca.markets/v2/options/contracts'
    records=[];tokens=set();seen=set()
    for _ in range(20):
        payload=client.json(url,params)
        chunk=payload.get('option_contracts')
        if not isinstance(chunk,list):
            raise ValueError('unexpected_option_contract_schema')
        for row in chunk:
            if row.get('underlying_symbol')!=underlying or row.get('symbol') in seen:
                raise ValueError('unexpected_option_contract_entity')
            seen.add(row['symbol'])
            oi=row.get('open_interest')
            if oi is not None and (not str(oi).isdigit() or int(oi)<0):
                raise ValueError('invalid_open_interest')
            oi_date=row.get('open_interest_date')
            if oi_date is not None:
                datetime.strptime(oi_date,'%Y-%m-%d')
            records.append(row)
        token=payload.get('next_page_token')
        if not token:
            break
        if token in tokens:
            raise ValueError('repeated_contract_page_token')
        tokens.add(token);params['page_token']=token
    else:
        raise ValueError('option_contracts_incomplete')
    if not records:
        raise ValueError('empty_option_contracts')
    metadata={'retrieved_at_utc':utcnow(),'source_url':url,'coverage':'active; expiry_next_35_days; strike_filter_if_provided',
              'strike_filter':[strike_min,strike_max],'historical_backfill':False,
              'open_interest_date_rule':'use_each_contract_open_interest_date; missing_is_unknown',
              'multiplier_rule':'use_provider_contract_fields; never_assume_100'}
    identifier=store.put('alpaca_option_contracts_oi',underlying,metadata,records)
    dates=sorted({r['open_interest_date'] for r in records if r.get('open_interest_date')})
    return {'source':'alpaca_option_contracts_oi:'+underlying,'status':'ok','dataset_id':identifier,
            'contracts':len(records),'contracts_with_open_interest':sum(r.get('open_interest') is not None for r in records),
            'open_interest_date_range':[dates[0],dates[-1]] if dates else None,'historical_backfill':False}


def collect_history(store, contracts, start, end, *, kind='bars', feed=None, client=None):
    if kind=='quotes':
        raise ValueError('historical_option_quotes_not_provided')
    if kind not in ('bars','trades') or not contracts or len(contracts)>100:
        raise ValueError('invalid_option_history_request')
    if any(not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}\d{6}[CP]\d{8}',x) for x in contracts):
        raise ValueError('invalid_occ_contract_symbol')
    began,ended=timestamp(start),timestamp(end)
    if began<datetime(2024,2,1,tzinfo=timezone.utc) or began>ended or ended>datetime.now(timezone.utc)-timedelta(minutes=20):
        raise ValueError('option_history_date_outside_available_range')
    if ended-began>timedelta(days=366 if kind=='bars' else 1):
        raise ValueError('option_history_window_too_large')
    if feed is not None:
        # bars/tradesの履歴APIにはfeed指定が文書化されていない。
        raise ValueError('option_history_feed_not_documented')
    params={'symbols':','.join(contracts),'start':start,'end':end,'limit':10000,'sort':'asc'}
    if kind=='bars':
        params['timeframe']='1Day'
    client=client or SourceHttp()
    groups={c:[] for c in contracts};tokens=set();url=BASE+kind
    for _ in range(30):
        payload=client.json(url,params)
        chunk=payload.get(kind)
        if chunk is None:
            chunk={}
        if not isinstance(chunk,dict) or any(c not in groups for c in chunk):
            raise ValueError('unexpected_option_history_schema')
        for contract,records in chunk.items():
            if not isinstance(records,list):
                raise ValueError('invalid_option_records')
            for row in records:
                point=timestamp(row['t'])
                # 日足時刻はNY日付の開始で、startを日中に置くと対象足より前の場合がある。
                if kind!='bars' and not began<=point<=ended:
                    raise ValueError('option_point_outside_request')
            groups[contract].extend(records)
        token=payload.get('next_page_token')
        if not token:
            break
        if token in tokens:
            raise ValueError('repeated_option_page_token')
        tokens.add(token);params['page_token']=token
    else:
        raise ValueError('option_history_incomplete')
    counts={c:len(v) for c,v in groups.items()}
    if not sum(counts.values()):
        raise ValueError('empty_option_history')
    for records in groups.values():
        points=[r['t'] for r in records]
        # 約定は同時刻の別約定が実在する。時刻重複だけで削除しない。
        if any(timestamp(a)>timestamp(b) for a,b in zip(points,points[1:])):
            raise ValueError('unordered_option_history')
    metadata={'retrieved_at_utc':utcnow(),'source_url':url,'start':start,'end':end,
              'kind':kind,'feed_requested':feed,'feed_identity':feed or 'not_exposed_by_documented_bars_api',
              'indicative_derivatives':feed=='indicative','contracts':contracts,'records_by_contract':counts,
              'complete_for_requested_filters':True,'historical_open_interest':False,
              'historical_implied_volatility':False,'option_contract_multiplier':'not_assumed; verify_contract_specification'}
    identifier=store.put('alpaca_option_history_'+kind,'MULTI_CONTRACT',metadata,groups)
    return {'source':'alpaca_option_history_'+kind,'status':'ok','dataset_id':identifier,
            'feed_requested':feed,'records':sum(counts.values()),'contracts':len(contracts)}


def collect_options(store, underlying):
    from .supplemental import safe_failure
    client=SourceHttp(max_requests=100)
    result=[];groups_by_feed={}
    # 保有中の値段ではなく、取得済みの参照株価から行使価格の調査範囲を絞る。
    ref=store.db.execute("SELECT id FROM datasets WHERE source='alpaca' AND symbol=? ORDER BY retrieved_at DESC LIMIT 1",(underlying,)).fetchone()
    strike_min=strike_max=None
    if ref:
        rows=store.prices(ref['id'])
        if rows:
            strike_min=round(rows[-1]['close']*.8,2);strike_max=round(rows[-1]['close']*1.2,2)
    try:
        result.append(collect_contracts(store,underlying,client=client,strike_min=strike_min,strike_max=strike_max))
    except Exception as exc:
        result.append(safe_failure('alpaca_option_contracts_oi:'+underlying,exc))
    for feed in ('opra','indicative'):
        try:
            identifier,groups=collect_chain(store,underlying,feed=feed,client=client,strike_min=strike_min,strike_max=strike_max)
            groups_by_feed[feed]=groups
            result.append({'source':'alpaca_option_chain:'+underlying,'feed_requested':feed,
                           'status':'ok','dataset_id':identifier,'contracts':len(groups),
                           'greek_contracts':sum(bool(x.get('greeks')) for x in groups.values()),
                           'historical_open_interest':False,'historical_implied_volatility':False})
        except Exception as exc:
            result.append({**safe_failure('alpaca_option_chain:'+underlying,exc),'feed_requested':feed})
    groups=groups_by_feed.get('opra') or groups_by_feed.get('indicative') or {}
    eligible=[]
    cutoff=datetime.now(timezone.utc)-timedelta(minutes=30)
    for contract,snapshot in groups.items():
        for field in ('latestTrade','latestQuote'):
            item=snapshot.get(field,{})
            if item.get('t') and timestamp(item['t'])<=cutoff:
                eligible.append((1 if field=='latestTrade' else 0,timestamp(item['t']),contract))
    if not eligible:
        result.append({'source':'alpaca_option_history:'+underlying,'status':'no_timestamped_contract_available'})
        return result
    _,point,contract=max(eligible)
    # 日中の一部や代表契約だけの取得を、過去チェーン全体の網羅と呼ばない。
    end=(point+timedelta(seconds=1)).isoformat()
    start=(point-timedelta(days=7)).replace(hour=0,minute=0,second=0,microsecond=0).isoformat()
    try:
        result.append(collect_history(store,[contract],start,end,client=client))
    except Exception as exc:
        result.append(safe_failure('alpaca_option_history_bars:'+underlying,exc))
    try:
        result.append(collect_history(store,[contract],(point-timedelta(seconds=30)).isoformat(),end,
                                      kind='trades',client=client))
    except Exception as exc:
        result.append(safe_failure('alpaca_option_history_trades:'+underlying,exc))
    result.append({'source':'alpaca_option_historical_quotes:'+underlying,'status':'not_provided_by_documented_api'})
    return result
