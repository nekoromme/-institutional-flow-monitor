"""固定した2023〜2024年・固定条件を保存入力で比較する。2025年は開かない。"""
from collections import Counter
from datetime import datetime,timezone
import json
import os
from statistics import mean,median
from flow_probe.bulk13f import digest
from flow_probe.pilot import score
from flow_probe.returns_core import schedule,summarize
from .adjustment_scan import load_development,DEVELOPMENT_YEARS
from .collect import ROOT,write_json,encrypt_bytes,decrypt_bytes
from .event_returns import shares_between,prepare_prices,window_valid,evaluate

MODELS=('abnormal','repeated','repeated_up','price_up_only')


def prepare_signals(payload,books,events,year):
    sessions=payload['sessions'];days=[s['date'] for s in sessions]
    if days!=sorted(set(days)):raise ValueError('invalid_calendar')
    full={s['date'] for s in sessions if s['open']=='09:30' and s['close']=='16:00'}
    prepared=[]
    for symbol,book in sorted(books.items()):
        prior=[]
        for i,day in enumerate(days):
            history=prior[-60:];reason=None
            if day not in full:reason='shortened_session'
            elif len(history)<60:reason='warmup'
            elif not window_valid(book,history+[day]):reason='missing_or_unreviewed_baseline'
            row={'symbol':symbol,'date':day,'status':reason or 'prepared',
                 'raw_volume':book[day]['raw']['v'] if day in book else None}
            if reason is None:
                row['baseline_volumes_in_decision_day_shares']=[book[d]['raw']['v']*shares_between(events,symbol,d,day) for d in history]
            prepared.append(row)
            if day in full:prior.append(day)
    scored=score(prepared,days,start=f'{year}-01-01',end=f'{year}-12-31')
    index={d:i for i,d in enumerate(days)}
    for row in scored:
        i=index[row['date']];book=books[row['symbol']];window=days[i-5:i+1] if i>=5 else []
        direction=None
        if window_valid(book,window):
            a,b=window[0],window[-1]
            direction=book[b]['raw']['c']*shares_between(events,row['symbol'],a,b)>book[a]['raw']['c']
        row['repeated_up']=(False if row['repeated'] is False else None if row['repeated'] is None or direction is None else direction)
        # 価格だけの比較相手にも、反復版が要求するデータ品質を課す。
        row['price_up_only']=direction if row['repeated'] is not None else None
    return scored,days


def run(root,secret):
    events=json.loads((root/'research/history/evidence/validation-events.json').read_text())['events']
    output={'plan_sha256':digest(root/'research/history/VALIDATION_PLAN.md'),'code_commit':os.environ.get('GITHUB_SHA','local'),
            'event_catalog_sha256':digest(root/'research/history/evidence/validation-events.json'),
            'reserved_year_opened':False,'reserved_year_returns_computed':False,
            'scope':'exploratory_identity_linked_candidates_not_complete_investment_universe','years':[]}
    private=[]
    for year in DEVELOPMENT_YEARS:
        payload=load_development(root,year,secret)
        books,quality=prepare_prices(payload,events)
        scored,days=prepare_signals(payload,books,events,year)
        results={};ledgers={};skips={}
        for model in MODELS:
            attempts,skipped=schedule(scored,days,model,10,start=f'{year}-01-01',end=f'{year}-12-31')
            ledger=evaluate(attempts,books,days,events)
            for r in ledger:
                if r['status']=='priced':r.update(net_excess_over_benchmark=None,dividend_adjusted_reference_net=None)
            summary=summarize(ledger)
            # 売買できなかった予定と、保有したが評価不明のものを分ける。
            summary['not_executable']=sum(r['status']=='not_executable' for r in ledger)
            summary['cash_entitlement_valuations']=sum(r.get('valuation_kind')=='cash_entitlement' for r in ledger)
            results[model]=summary;ledgers[model]=ledger;skips[model]=skipped
        report={'year':year,'requested_symbols':len(books),'price_quality':quality,'summaries':results,'skips':skips,
                'signal_counts':{m:{'true':sum(r[m] is True for r in scored),'unknown':sum(r[m] is None for r in scored)} for m in MODELS},
                'trade_ledgers':ledgers,'main_holding_sessions':10,'cost_each_side':.0025,
                'cash_dividends_included':False,'benchmark_comparison_performed':False,'actual_execution_verified':False}
        output['years'].append(report);private.append({'year':year,'scored':scored,'trade_ledgers':ledgers})
        print(json.dumps({'year':year,'results':{m:{k:v[k] for k in ('attempts','priced','mean_net_return','median_net_return','net_win_fraction','cash_entitlement_valuations')} for m,v in results.items()}}),flush=True)
    raw=json.dumps(private,sort_keys=True,allow_nan=False).encode();encrypted=encrypt_bytes(raw,secret)
    if decrypt_bytes(encrypted,secret)!=raw:raise ValueError('validation_storage_roundtrip_failed')
    dest=root/'data/history/encrypted/validation-ledgers.enc';dest.write_bytes(encrypted)
    output['private_ledger_encrypted_sha256']=digest(dest)
    write_json(root/'diagnostics/history/validation/development-results.json',output)
    return output

if __name__=='__main__':run(ROOT,os.environ.get('ALPACA_SECRET_KEY',''))
