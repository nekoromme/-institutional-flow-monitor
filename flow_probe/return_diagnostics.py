"""54候補の通知後損益を取得・計算し、公開してよい派生集計を保存する。

日足の参考損益を実際に執行した利益と混同しない。秘密・原価格・原出来高は
ログへ出さない。追加の保有報告を待たず、既存の通知をそのまま評価する。
"""
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys

from .__main__ import assert_no_secrets
from .alpaca import NY, fetch_pages, split_adjustment_audit
from .batch_common import load_protocol, PROTOCOL_SHA
from .bulk13f import digest
from .http_client import SafeHttp, ProbeError
from .returns_core import (by_date, valid_bar, schedule, evaluate, summarize,
                           failure_slices, wait_pairs, HORIZONS)

START, END = '2025-12-15', '2026-04-30'
PLAN = 'docs/RETURN_DIAGNOSTIC_PROTOCOL.md'


def validate_price_calendar(sessions):
    """通知後の追跡用。旧試行の3月末限定の暦検査とは別に範囲を固定する。"""
    if not isinstance(sessions,list) or not sessions:raise ProbeError('invalid_return_calendar')
    days=[]
    for row in sessions:
        if not isinstance(row,dict):raise ProbeError('invalid_return_session')
        day=row.get('date','');date.fromisoformat(day)
        if not START<=day<=END or row.get('open')!='09:30' or row.get('close') not in {'13:00','16:00'}:
            raise ProbeError('unreviewed_return_session')
        days.append(day)
    if days!=sorted(set(days)):raise ProbeError('invalid_return_calendar_order')
    return days


def object_hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()


def prepare_books(data, sessions, source, through):
    """照合済みの日だけを価格評価に使う。以前の通知入力との差は隠さない。"""
    days={s['date'] for s in sessions};books={};quality=[]
    audits=split_adjustment_audit(data['raw'],data['split'],complete=True,through=through)
    for symbol, raw in data['raw'].items():
        a,b,c=[by_date(data[k].get(symbol,[])) for k in ('raw','split','split,dividend')]
        audit=audits[symbol];unresolved=set(audit['unresolved_days']);changed=[]
        previous=source.get(symbol,{}).get('raw',{}).get(symbol,[])
        for day,old in by_date(previous).items():
            if START <= day <= '2026-03-31':
                now=a.get(day)
                if not now or any(now.get(k)!=old.get(k) for k in ('o','h','l','c','v')):changed.append(day)
        # 将来価格の再取得で通知入力まで変わった場合、今回は自動更新しない。
        globally_held=bool(changed or not audit['coverage_complete'] or any(d not in days for d in a))
        book={}
        for day,row in a.items():
            split=b.get(day);dividend=c.get(day)
            valid=bool(not globally_held and day not in unresolved and valid_bar(row) and valid_bar(split))
            dividend_ok=valid and valid_bar(dividend)
            if dividend_ok:
                factor=dividend['c']/split['c']
                dividend_ok=all(math.isclose(dividend[k],split[k]*factor,rel_tol=1e-6,abs_tol=0.0002)
                                for k in ('o','h','l','c'))
            book[day]={'valid':valid,'raw':row,'split':split,
                       'dividend':dividend if dividend_ok else None}
        books[symbol]=book
        quality.append({'symbol':symbol,'raw_days':len(a),'valid_price_days':sum(r['valid'] for r in book.values()),
                        'unresolved_adjustment_days':sorted(unresolved), 'original_signal_input_changed_dates':changed,
                        'source_pairing_complete':audit['coverage_complete'],'global_price_hold':globally_held,
                        'dividend_reference_days':sum(r['dividend'] is not None for r in book.values())})
    return books,quality


def minute_checks(client, trials, targets, sessions, books):
    """事前固定した標本で日足と周辺1分を照合。価格を差し替えない。"""
    session={r['date']:r for r in sessions};selected=[]
    for model in ('abnormal','repeated'):
        earliest={}
        for row in trials[model]:
            earliest.setdefault(row['symbol'],row)
        all_rows=sorted(earliest.values(),key=lambda r:(r['signal_date'],r['symbol']))
        low=[r for r in all_rows if targets[r['symbol']]['primary_low_reported_ratio']]
        other=[r for r in all_rows if not targets[r['symbol']]['primary_low_reported_ratio']][:5]
        selected.extend(low+other)
    cache={};checks=[]
    for row in selected:
        for leg,field in [('entry','o'),('exit','c')]:
            symbol=row['symbol'];day=row[leg+'_date']
            if day is None:continue
            key=(symbol,day,leg)
            if key not in cache:
                clock=session[day]['open' if leg=='entry' else 'close']
                start=datetime.fromisoformat(day+'T'+clock).replace(tzinfo=NY)
                if leg=='exit':start-=timedelta(minutes=1)
                bars,meta=fetch_pages(client,'bars',(symbol,),start,start+timedelta(minutes=1)-timedelta(microseconds=1),
                                     feed='sip',timeframe='1Min',limit=1000,max_pages=1)
                items=bars[symbol];daily=books.get(symbol,{}).get(day,{}).get('raw')
                paired=bool(meta['complete'] and len(items)==1 and valid_bar(items[0]) and valid_bar(daily))
                match=math.isclose(items[0][field],daily[field],rel_tol=1e-6,abs_tol=0.0001) if paired else None
                cache[key]={'symbol':symbol,'date':day,'leg':leg,'complete':meta['complete'],
                            'minute_rows':len(items),'daily_reference_matches_neighbor_minute':match,
                            'raw_source_sha256':object_hash(items)}
            checks.append({'model':row['model'],**cache[key]})
    return {'checks':checks,'unique_requests':len(cache),
            'matched':sum(r['daily_reference_matches_neighbor_minute'] is True for r in checks),
            'different':sum(r['daily_reference_matches_neighbor_minute'] is False for r in checks),
            'unknown':sum(r['daily_reference_matches_neighbor_minute'] is None for r in checks),
            'actual_execution_confirmed':False,
            'limitation':'sample_only; no_size_or_quote_check; closing_auction_can_differ_from_prior_minute',
            'private_responses':cache}


def run(root,client):
    protocol=load_protocol(root);targets={r['symbol']:r for r in protocol['symbols']}
    source_path=root/'data/batch-market/market-input-and-scores.json'
    source=json.loads(source_path.read_text())
    market=json.loads((root/'diagnostics/batch-market.json').read_text())
    if market['protocol_sha256']!=PROTOCOL_SHA or digest(source_path)!=market['market_input_and_scores_sha256']:
        raise ValueError('return_signal_input_mismatch')
    sessions=client.json('https://paper-api.alpaca.markets/v2/calendar',{'start':START,'end':END})
    days=validate_price_calendar(sessions)
    if [d for d in days if d<='2026-03-31'] != [s['date'] for s in source['sessions'] if s['date']>=START]:
        raise ValueError('return_calendar_changed')
    retrieved=datetime.now(timezone.utc);symbols=tuple(targets)+('SPY',)
    start=datetime.fromisoformat(START).replace(tzinfo=NY)
    end=datetime.fromisoformat(END+'T23:59:59').replace(tzinfo=NY)
    data={};probes=[]
    for adjustment in ('raw','split','split,dividend'):
        rows,meta=fetch_pages(client,'bars',symbols,start,end,feed='sip',timeframe='1Day',
                              adjustment=adjustment,limit=10000,max_pages=3)
        data[adjustment]=rows;probes.append(meta)
        if not meta['complete']:raise ProbeError('return_market_download_incomplete')
    books,quality=prepare_books(data,sessions,source['symbols'],retrieved.astimezone(NY).date().isoformat())
    scored=[];coverage=[]
    evaluation_days=[d for d in days if '2026-01-01'<=d<='2026-03-31']
    for symbol in targets:
        existing=source['symbols'].get(symbol,{}).get('scored',[])
        by_day={r['date']:r for r in existing}
        rows=[by_day.get(d,{'symbol':symbol,'date':d,'abnormal':None,'repeated':None}) for d in evaluation_days]
        scored.extend(rows)
        coverage.append({'symbol':symbol,'sessions':len(rows),
                         'unknown_abnormal':sum(r['abnormal'] is None for r in rows),
                         'unknown_repeated':sum(r['repeated'] is None for r in rows),
                         'abnormal_days':sum(r['abnormal'] is True for r in rows),
                         'repeated_days':sum(r['repeated'] is True for r in rows)})
    results={};ledgers={};skips={};first_attempts=None
    for horizon in HORIZONS:
        results[str(horizon)]={};ledgers[str(horizon)]={};skips[str(horizon)]={}
        for field in ('abnormal','repeated'):
            attempts,skipped=schedule(scored,days,field,horizon)
            rows=evaluate(attempts,books,days)
            ledgers[str(horizon)][field]=rows;skips[str(horizon)][field]=skipped
            if horizon==10 and field=='abnormal':first_attempts=attempts
            results[str(horizon)][field]={}
            for group in ('all','primary_low_reported_ratio','relative_lower_third'):
                chosen=rows if group=='all' else [r for r in rows if targets[r['symbol']][group]]
                results[str(horizon)][field][group]=summarize(chosen)
    pairing=wait_pairs(first_attempts,scored,books,days)
    execution=minute_checks(client,ledgers['10'],targets,sessions,books)
    private_execution=execution.pop('private_responses')
    folder=root/'data/return-diagnostics';folder.mkdir(parents=True,exist_ok=True)
    payload=json.dumps({'sessions':sessions,'prices':data,'ledgers':ledgers,
                        'minute_check_records':list(private_execution.values())},sort_keys=True,allow_nan=False).encode()
    (folder/'inputs-and-ledgers.json').write_bytes(payload)
    return {'version':'return-diagnostics-0.1','code_commit':os.environ.get('GITHUB_SHA','local'),
            'plan_sha256':digest(root/PLAN),'base_protocol_sha256':PROTOCOL_SHA,
            'signal_dataset_sha256':digest(source_path),'private_inputs_sha256':hashlib.sha256(payload).hexdigest(),
            'retrieved_at_utc':retrieved.isoformat(),'price_history':{'start':START,'end':END},
            'requested_candidates':len(targets),'benchmark':'SPY','probes':probes,'price_quality':quality,
            'signal_coverage':coverage,'skips':skips,'summaries':results,
            'failure_slices_10':{m:failure_slices(v) for m,v in ledgers['10'].items()},
            'waiting_for_repetition':pairing,'minute_reference_checks':execution,
            # 10日だけは各試行の日付・損益・不明理由も公開し、平均値を追跡できる。
            'trade_ledger_10':ledgers['10'], 'primary_cost_each_side':0.0025,
            'unit':'equal_money_per_attempt_price_reference_not_portfolio_return',
            'primary_result_excludes_cash_dividends':True,'actual_execution_verified':False,
            'independent_period_validated':False,'parameter_optimization_performed':False,
            'limitations':['exploratory_2026_Q1','daily_reference_not_fill_price','fees_and_spread_assumed_not_measured',
                           'dividend_adjusted_proxy_not_cash_distribution_accounting','unresolved_outcomes_not_zero',
                           'trades_across_symbols_and_dates_dependent','industry_size_matched_controls_not_implemented',
                           'raw_inputs_private_runner_only_expire_after_job']}


def main():
    root=Path(__file__).resolve().parents[1]
    key=os.environ.get('ALPACA_API_KEY','').strip();secret=os.environ.get('ALPACA_SECRET_KEY','').strip()
    client=SafeHttp(key,secret,timeout=25,max_requests=100,max_seconds=300)
    try:
        if not key or not secret:raise ProbeError('missing_github_secrets')
        result=run(root,client)
    except Exception as exc:
        result={'version':'return-diagnostics-0.1','error':exc.summary() if isinstance(exc,ProbeError)
                else {'category':'unexpected_'+type(exc).__name__}}
    result['http']=client.metrics()
    text=json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)
    assert_no_secrets(text,[key,secret])
    (root/'diagnostics').mkdir(exist_ok=True)
    (root/'diagnostics/return-diagnostics.json').write_text(text+'\n')
    print('RETURN_DIAGNOSTICS_BEGIN');print(text);print('RETURN_DIAGNOSTICS_END')
    return 2 if 'error' in result else 0


if __name__=='__main__':sys.exit(main())
