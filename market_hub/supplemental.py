"""無料で使える公式CSV・韓国株日足と、任意キーの接続をまとめる。

最新改定値を当時の値に見せかけず、未確認のカレンダーを営業日一覧にしない。
公開するのは成否と件数だけ。データ本体は共通DB→暗号化キャッシュへ。
"""
import csv
import io
import json
import math
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree
from zoneinfo import ZoneInfo
from flow_probe.http_client import ProbeError
from .source_access import SourceHttp
from .store import utcnow, validate_bars


def iso_day(value):
    if not re.fullmatch(r'\d{4}-\d\d-\d\d',value):
        raise ValueError('invalid_date')
    return date.fromisoformat(value)


def collect_fred_csv(store, series, start, end, *, client=None):
    """最新公表版だけ。過去の意思決定用データとしては自動採用しない。"""
    if not re.fullmatch(r'[A-Za-z0-9_]{1,80}',series):
        raise ValueError('invalid_series')
    if iso_day(start)>iso_day(end):
        raise ValueError('invalid_macro_dates')
    url='https://fred.stlouisfed.org/graph/fredgraph.csv'
    raw=(client or SourceHttp()).read(url,{'id':series,'cosd':start,'coed':end})
    text=raw.decode('utf-8-sig')
    reader=csv.DictReader(io.StringIO(text))
    if reader.fieldnames not in (['observation_date',series],['DATE',series]):
        raise ValueError('unexpected_fred_csv_columns')
    date_field=reader.fieldnames[0]
    rows=[];seen=set();outside=0
    for record in reader:
        day=record[date_field]
        iso_day(day)
        if day in seen:
            raise ValueError('duplicate_macro_date')
        seen.add(day)
        if not start<=day<=end:
            # 月次・四半期グラフは期間境界をその頻度に拡張する場合がある。
            # 原CSVを保存したうえで指定範囲へ明示的に切り出す。
            outside+=1
            continue
        value=record[series]
        number=None if value in ('','.') else float(value)
        if number is not None and not math.isfinite(number):
            raise ValueError('invalid_macro_number')
        rows.append({'date':day,'value':number})
    if not rows:
        raise ValueError('empty_macro_data')
    metadata={'retrieved_at_utc':utcnow(),'source_url':url,'series_url':'https://fred.stlouisfed.org/series/'+series,
              'start':start,'end':end,'vintage':None,'revision_mode':'latest_published',
              'point_in_time_usable':False,'outside_requested_range_filtered':outside,'missing_values':'retained_as_null; never_forward_filled',
              'units':'see_official_series_metadata; no_automatic_unit_conversion'}
    identifier=store.put('fred_public_csv',series,metadata,{'csv':text,'observations':rows})
    return {'source':'fred_public_csv','series':series,'status':'ok','dataset_id':identifier,
            'rows':len(rows),'missing_values':sum(r['value'] is None for r in rows),'point_in_time_usable':False}


def collect_korea_naver(store, symbol, *, count=1500, client=None):
    """NAVERが公開するチャートから日足を取得。KRX公式API接続とは区別する。"""
    if not re.fullmatch(r'\d{6}',symbol) or not 1<=count<=6000:
        raise ValueError('invalid_korean_symbol_or_count')
    url='https://fchart.stock.naver.com/sise.nhn'
    raw=(client or SourceHttp()).read(url,{'symbol':symbol,'timeframe':'day','count':count,'requestType':'0'})
    # 外部実体を解決しない。未知のDTDを含む入力は利用しない。
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('unexpected_xml_declaration')
    text=raw.decode('euc-kr')
    root=ElementTree.fromstring(text)
    chart=root.find('chartdata')
    if chart is None or chart.get('symbol')!=symbol or chart.get('timeframe')!='day':
        raise ValueError('unexpected_naver_entity')
    all_rows=[];seen=set();invalid=0
    today=datetime.now(ZoneInfo('Asia/Seoul')).date().isoformat()
    for item in chart.findall('item'):
        values=item.get('data','').split('|')
        if len(values)!=6 or not re.fullmatch(r'\d{8}',values[0]):
            raise ValueError('unexpected_naver_row')
        day=date(int(values[0][:4]),int(values[0][4:6]),int(values[0][6:8])).isoformat()
        if day in seen:
            raise ValueError('duplicate_korean_date')
        seen.add(day)
        if day>=today:
            continue  # 進行中の足を含めない。
        row={'date':day,**{k:float(v) for k,v in zip(('open','high','low','close','volume'),values[1:])}}
        try:
            validate_bars([row],[day])
        except ValueError:
            invalid+=1
            continue
        all_rows.append(row)
    rows=sorted(all_rows,key=lambda r:r['date'])
    if not rows:
        raise ValueError('empty_korean_prices')
    # 正式な営業日カレンダーは未取得。観測日一覧をカレンダーだと偽らない。
    metadata={'retrieved_at_utc':utcnow(),'source_url':url,'start':rows[0]['date'],'end':rows[-1]['date'],
              'currency':'KRW','exchange_timezone':'Asia/Seoul','feed':'naver_public_chart',
              'adjustment':'unknown','timeframe':'1Day','session_scope':'provider_daily',
              'sessions':[r['date'] for r in rows],'calendar_verified':False,
              'calendar_source':'observed_dates_only_not_exchange_calendar','invalid_rows_quarantined':invalid,
              'point_in_time_adjustments':False,'cash_dividends_included':False,
              'event_study_eligible':False,'limitation':'adjustments and KRX calendar require independent verification'}
    identifier=store.put('naver_korea',symbol,metadata,{'xml':text},rows)
    return {'source':'naver_korea','symbol':symbol,'status':'ok','dataset_id':identifier,
            'rows':len(rows),'invalid_rows_quarantined':invalid,'start':rows[0]['date'],'end':rows[-1]['date'],
            'event_study_eligible':False}


def collect_retail_top10(store, start, end, *, client=None):
    """Nasdaq無料版。活動比率と10日センチメントを当日の純買付額に変換しない。"""
    key=os.environ.get('NASDAQ_DATA_LINK_API_KEY','')
    if not key:
        return {'source':'nasdaq_rtat10','status':'not_configured','required_secret':'NASDAQ_DATA_LINK_API_KEY'}
    if iso_day(start)>iso_day(end):
        raise ValueError('invalid_retail_dates')
    url='https://data.nasdaq.com/api/v3/datatables/NDAQ/RTAT10.json'
    params={'api_key':key,'date.gte':start,'date.lte':end,'qopts.per_page':10000}
    client=client or SourceHttp()
    rows=[];columns=None;tokens=set()
    for _ in range(20):
        payload=client.json(url,params)
        table=payload.get('datatable',{})
        current=[c.get('name') for c in table.get('columns',[])]
        if not {'date','ticker','activity','sentiment'}<=set(current) or (columns is not None and current!=columns):
            raise ValueError('unexpected_retail_columns')
        columns=current
        for values in table.get('data',[]):
            if len(values)!=len(columns):
                raise ValueError('unexpected_retail_row')
            row=dict(zip(columns,values))
            if not start<=row['date']<=end:
                raise ValueError('retail_date_outside_request')
            if not 0<=row['activity']<=1 or not -100<=row['sentiment']<=100:
                raise ValueError('invalid_retail_measurement')
            rows.append(row)
        token=payload.get('meta',{}).get('next_cursor_id')
        if not token:
            break
        if token in tokens:
            raise ValueError('repeated_retail_cursor')
        tokens.add(token);params['qopts.cursor_id']=token
    else:
        raise ValueError('retail_page_limit')
    if not rows:
        return {'source':'nasdaq_rtat10','status':'empty_no_inference'}
    metadata={'retrieved_at_utc':utcnow(),'source_url':url,'start':start,'end':end,
              'measurement':'estimated_from_SIP','coverage':'top_10_by_activity_not_full_market',
              'activity_definition':'share_of_retail_dollar_turnover_across_all_tickers',
              'sentiment_definition':'derived_from_most_recent_10_trading_day_net_flows',
              'not_available':['daily_net_buy_dollars','full_universe','actor_identity_ground_truth']}
    identifier=store.put('nasdaq_rtat10','TOP10',metadata,rows)
    return {'source':'nasdaq_rtat10','status':'ok','dataset_id':identifier,'rows':len(rows),'latest_observation_date':max(r['date'] for r in rows),
            'requested_end':end,'publication_lag_possible':True}


def safe_failure(source, exc):
    result={'source':source,'status':'error','category':'source_operation_failed'}
    if isinstance(exc,ProbeError):
        result.update(exc.summary())
    elif isinstance(exc,ValueError) and re.fullmatch('[a-z_]{3,80}',str(exc)):
        result['category']=str(exc)
    return result


def collect_supplemental(store, config_path):
    from .options_data import collect_options, collect_history
    from .providers import collect_macro
    config=json.loads(Path(config_path).read_text())
    today=date.today();end=(today-timedelta(days=1)).isoformat()
    start=(today-timedelta(days=config.get('macro_lookback_days',3653))).isoformat()
    result=[]
    # 一つが失敗しても、関係のない取得口まで止めない。
    for series in config.get('fred_series',[]):
        try:
            result.append(collect_fred_csv(store,series,start,end))
        except Exception as exc:
            result.append(safe_failure('fred_csv:'+series,exc))
    for symbol in config.get('korean_symbols',[]):
        try:
            result.append(collect_korea_naver(store,symbol))
        except Exception as exc:
            result.append(safe_failure('naver:'+symbol,exc))
    # キーが既にあれば当時版APIも実測。キーの値自体は読まない・表示しない。
    if os.environ.get('FRED_API_KEY'):
        try:
            requests=config.get('fred_vintage_requests') or [{'series':'DGS10','start':start,'end':end,'vintage':today.isoformat()}]
            for request in requests:
                identifier=collect_macro(store,request['series'],request['start'],request['end'],request['vintage'])
                data=store.dataset(identifier)
                result.append({'source':'fred_vintage_api','status':'ok','dataset_id':identifier,
                    'series':request['series'],'vintage':request['vintage'],'rows':len(data['payload'])})
        except Exception as exc:
            result.append(safe_failure('fred_vintage_api',exc))
    else:
        result.append({'source':'fred_vintage_api','status':'not_configured','required_secret':'FRED_API_KEY'})
    for symbol in config.get('option_underlyings',[]):
        try:
            result.extend(collect_options(store,symbol))
        except Exception as exc:
            result.append(safe_failure('alpaca_options:'+symbol,exc))
    for request in config.get('option_history_requests',[]):
        try:
            result.append(collect_history(store,request['contracts'],request['start'],request['end'],kind=request.get('kind','bars')))
        except Exception as exc:
            result.append(safe_failure('alpaca_requested_option_history',exc))
    mode=config.get('retail_flow_mode','optional_nasdaq_api')
    if mode=='public_reports':
        # 公開記事は検索・本文照合が必要。数値を機械で作ったり、ゼロで埋めたりしない。
        result.append({'source':'retail_public_research','status':'external_research',
                       'signup_required':False,'workflow':'existing_morning_retail_flow_research',
                       'coverage':'public_reports_only; not_a_complete_daily_panel'})
        result.append({'source':'nasdaq_rtat10','status':'disabled_by_preference'})
    elif mode=='optional_nasdaq_api':
        try:
            result.append(collect_retail_top10(store,(today-timedelta(days=35)).isoformat(),end))
        except Exception as exc:
            result.append(safe_failure('nasdaq_rtat10',exc))
    else:
        result.append({'source':'retail_mode','status':'error','category':'invalid_retail_mode'})
    return {'retrieved_at_utc':utcnow(),'sources':result,'raw_data_public':False,
            'status':'partial' if any(r['status']=='error' for r in result) else 'ok',
            'note':'Each source has its own status; aggregate success is not proof that every source works.'}
