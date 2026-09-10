"""別期間で変更案を試す。買う予定から再計算し、良い取引だけを抜かない。

原価格を公開せず、判定・損益・不明理由・入力のハッシュを残す。
旧期間は探索、新期間は固定した案の追試として別々に出力する。
"""
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from .__main__ import assert_no_secrets
from .alpaca import NY, fetch_pages, quality, split_adjustment_audit
from .batch_common import load_protocol, PROTOCOL_SHA
from .bulk13f import digest
from .execution_followup import inspect_execution, dividend_shapes
from .http_client import SafeHttp, ProbeError
from .pilot import score
from .reference import documented_price_factor
from .research import prepare_daily
from .return_diagnostics import prepare_books, validate_price_calendar, object_hash
from .returns_core import schedule, evaluate, summarize, failure_slices, HORIZONS

HISTORY_START, HISTORY_END = '2025-12-15', '2026-07-31'
MODELS = ('abnormal','repeated','repeated_up')
PLAN = 'docs/NEXT_RETURN_VALIDATION_PLAN.md'
METHOD = 'docs/NEXT_RETURN_METHOD.md'


def add_price_direction(scored, books, days):
    """既知の当日までの価格だけで、上昇方向を追加する。

    未来の分割補正済み価格を特徴量へ直接使わない。過去の生の価格を、
    当日までの正式な分割・併合だけで同じ単位に換算する。
    """
    index={d:i for i,d in enumerate(days)};output=[]
    for original in scored:
        row=dict(original);d=row['date'];i=index[d];book=books.get(row['symbol'],{})
        window=days[i-5:i+1] if i>=5 else []
        known=len(window)==6 and all(book.get(t,{}).get('valid') for t in window)
        change=None
        if known:
            old=book[window[0]]['raw']['c']*documented_price_factor(row['symbol'],window[0],d)
            change=book[d]['raw']['c']/old-1
        row['known_five_day_price_change']=change
        if row['repeated'] is False:
            row['repeated_up']=False;row['direction_reason']=None
        elif row['repeated'] is None:
            row['repeated_up']=None;row['direction_reason']='repetition_unknown'
        elif change is None:
            row['repeated_up']=None;row['direction_reason']='five_day_price_window_unknown'
        else:
            row['repeated_up']=change>0;row['direction_reason']=None
        output.append(row)
    return output


def compare_period(scored, books, days, targets, start, end):
    """全モデルを別々に予定購入から作り、同じ購入制限で評価する。"""
    selected=[r for r in scored if start<=r['date']<=end]
    summaries={};ledgers={};skips={}
    for horizon in HORIZONS:
        summaries[str(horizon)]={};ledgers[str(horizon)]={};skips[str(horizon)]={}
        for model in MODELS:
            attempts,counts=schedule(selected,days,model,horizon,start=start,end=end)
            rows=evaluate(attempts,books,days)
            ledgers[str(horizon)][model]=rows;skips[str(horizon)][model]=counts
            summaries[str(horizon)][model]={group:summarize(rows if group=='all' else
                [r for r in rows if targets[r['symbol']][group]])
                for group in ('all','primary_low_reported_ratio','relative_lower_third')}
    months=sorted({r['date'][:7] for r in selected})
    coverage=[]
    for symbol in targets:
        rows=[r for r in selected if r['symbol']==symbol]
        coverage.append({'symbol':symbol,'sessions':len(rows),
                         'signals':{m:sum(r[m] is True for r in rows) for m in MODELS},
                         'unknown':{m:sum(r[m] is None for r in rows) for m in MODELS},
                         'unknown_reasons':dict(Counter(r.get('reason') for r in rows if r['abnormal'] is None))})
    public={'start':start,'end':end,'coverage':coverage,'summaries':summaries,'skips':skips,
            'failure_slices_10':{m:failure_slices(v,months=months) for m,v in ledgers['10'].items()},
            'trade_ledger_10':ledgers['10'],'scored_sha256':object_hash(selected)}
    return public,ledgers


def run(root,client):
    protocol=load_protocol(root);targets={r['symbol']:r for r in protocol['symbols']}
    # 同じ実行で先に取得した旧期間の価格・通知と対応づける。
    prior_path=root/'data/return-diagnostics/inputs-and-ledgers.json'
    prior=json.loads(prior_path.read_text())
    prior_report=json.loads((root/'diagnostics/return-diagnostics.json').read_text())
    if digest(prior_path)!=prior_report['private_inputs_sha256']:
        raise ValueError('old_return_inputs_changed')
    batch_path=root/'data/batch-market/market-input-and-scores.json'
    batch=json.loads(batch_path.read_text())
    if digest(batch_path)!=prior_report['signal_dataset_sha256']:
        raise ValueError('old_signal_inputs_changed')
    sessions=client.json('https://paper-api.alpaca.markets/v2/calendar',
                         {'start':HISTORY_START,'end':HISTORY_END})
    days=validate_price_calendar(sessions,start=HISTORY_START,end=HISTORY_END)
    if [s for s in sessions if s['date']<='2026-04-30']!=prior['sessions']:
        raise ValueError('next_calendar_differs_from_prior')
    now=datetime.now(timezone.utc);symbols=tuple(targets)+('SPY',)
    begin=datetime.fromisoformat(HISTORY_START).replace(tzinfo=NY)
    finish=datetime.fromisoformat(HISTORY_END+'T23:59:59').replace(tzinfo=NY)
    data={};probes=[]
    for adjustment in ('raw','split','split,dividend'):
        rows,meta=fetch_pages(client,'bars',symbols,begin,finish,feed='sip',timeframe='1Day',
                              adjustment=adjustment,limit=10000,max_pages=3)
        if not meta['complete']:raise ProbeError('next_market_download_incomplete')
        data[adjustment]=rows;probes.append(meta)
    old={s:{'raw':{s:rows}} for s,rows in prior['prices']['raw'].items()}
    books,price_quality=prepare_books(data,sessions,old,now.astimezone(NY).date().isoformat(),
                                      overlap_start=HISTORY_START,overlap_end='2026-04-30')
    # 足の不正な並び・範囲外データも無視しない。
    q=quality(data['raw'],'bars',sessions,timeframe='1Day',complete=True,start=begin,end=finish)
    audit=split_adjustment_audit(data['raw'],data['split'],complete=True,through=now.astimezone(NY).date().isoformat())
    raw={s:data['raw'][s] for s in targets}
    for s in raw:
        if any(q[s][k] for k in ('invalid_records','identical_rows_or_duplicate_bar_times','out_of_order','outside_requested_interval')):
            audit[s]['coverage_complete']=False;audit[s]['status']='needs_review'
            for b in books[s].values():b['valid']=False
    prepared,_=prepare_daily(raw,sessions,audit)
    later=score(prepared,days,start='2026-05-01',end='2026-06-30')
    # 旧期間の出来高は9月以降の60日窓から既に採点した版をそのまま使う。
    earlier=[]
    for s in targets:
        known={r['date']:r for r in batch['symbols'].get(s,{}).get('scored',[])}
        earlier.extend(known.get(d,{'symbol':s,'date':d,'abnormal':None,'repeated':None,'reason':'old_signal_unknown'})
                       for d in days if '2026-01-01'<=d<='2026-03-31')
    old_scored=add_price_direction(earlier,books,days)
    new_scored=add_price_direction(later,books,days)
    exploration,old_ledgers=compare_period(old_scored,books,days,targets,'2026-01-01','2026-03-31')
    validation,new_ledgers=compare_period(new_scored,books,days,targets,'2026-05-01','2026-06-30')
    # 期間パラメータを外へ出す変更が、旧版の損益を変えていないか確認する。
    unchanged=all(exploration['summaries'][h][m]==prior_report['summaries'][h][m]
                  for h in ('5','10','20') for m in ('abnormal','repeated'))
    execution,private_execution=inspect_execution(client,books)
    shape=dividend_shapes(data)
    folder=root/'data/next-returns';folder.mkdir(parents=True,exist_ok=True)
    body=json.dumps({'sessions':sessions,'prices':data,'new_scored':new_scored,'old_scored':old_scored,
                     'old_ledgers':old_ledgers,'new_ledgers':new_ledgers,'execution':private_execution},
                     sort_keys=True,allow_nan=False).encode()
    (folder/'inputs-and-ledgers.json').write_bytes(body)
    return {'version':'next-returns-0.1','code_commit':os.environ.get('GITHUB_SHA','local'),
            'protocol_sha256':PROTOCOL_SHA,'plan_sha256':digest(root/PLAN),'method_sha256':digest(root/METHOD),
            'retrieved_at_utc':now.isoformat(),'private_inputs_sha256':digest(folder/'inputs-and-ledgers.json'),
            'old_signal_inputs_sha256':digest(batch_path),'old_returns_unchanged':unchanged,
            'probes':probes,'price_quality':price_quality,'exploration':exploration,'validation':validation,
            'execution_followup':execution,'dividend_shapes':shape,
            'primary_cost_each_side':0.0025,'primary_holding_sessions':10,'models':list(MODELS),
            'actual_execution_verified':False,'primary_result_excludes_cash_dividends':True,
            'parameter_grid_search_performed':False,'new_candidate_count':1,
            'limitations':['fixed_54_proxy_cohort_not_market_representative','dependent_trades_not_independent_samples',
                           'chronological_followup_not_pristine_final_holdout','prices_not_executable_fills',
                           'unresolved_signals_retained','corporate_symbol_changes_not_automatically_spliced',
                           'no_size_sector_matched_non_signal_control','private_raw_inputs_expire_with_runner']}


def main():
    root=Path(__file__).resolve().parents[1]
    key=os.environ.get('ALPACA_API_KEY','').strip();secret=os.environ.get('ALPACA_SECRET_KEY','').strip()
    client=SafeHttp(key,secret,timeout=25,max_requests=80,max_seconds=240)
    try:
        if not key or not secret:raise ProbeError('missing_github_secrets')
        result=run(root,client)
    except Exception as exc:
        result={'version':'next-returns-0.1','error':exc.summary() if isinstance(exc,ProbeError)
                else {'category':'unexpected_'+type(exc).__name__}}
    result['http']=client.metrics();text=json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)
    assert_no_secrets(text,[key,secret]);(root/'diagnostics').mkdir(exist_ok=True)
    (root/'diagnostics/next-returns.json').write_text(text+'\n')
    print('NEXT_RETURNS_BEGIN');print(text);print('NEXT_RETURNS_END')
    return 2 if 'error' in result else 0


if __name__=='__main__':sys.exit(main())
