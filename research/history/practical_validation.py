"""提供元の分割調整を採用した暫定検証。原書類の全件確認を待たない。

既知の問題14銘柄は結果を見る前に固定除外する。この選択には事後情報が
含まれるため、一般の投資対象全体を再現した成績とは主張しない。
"""
import json
import os
from statistics import median,mean
from flow_probe.bulk13f import digest
from flow_probe.returns_core import by_date,valid_bar,schedule,evaluate,summarize
from .collect import ROOT,write_json,encrypt_bytes,decrypt_bytes
from .gap_audit import load_expanded
from .development_validation import prepare_signals,MODELS

EXCLUDED=set('AGFS CTG FSTX CTLT LAZY AVYA CSPI EVLO ICCH KBNT SRPT UTRS B INBX'.split())
VARIANTS=('repeated_up_capped','repeated_up_liquid')


def prepare(payload):
    """分割調整済み価格・出来高を同じ単位で使い、二重補正しない。

    基準期間と当日の比率を使うため、将来の分割による一律の単位変更は
    シグナルを変えない。ドル売買代金だけは当時の未調整価格×出来高を使う。
    """
    books={};audit={};sessions={s['date'] for s in payload['sessions']}
    for symbol,bars in sorted(payload['prices']['split'].items()):
        if symbol in EXCLUDED:continue
        adjusted=by_date(bars);raw=by_date(payload['prices']['raw'][symbol])
        dividend=by_date(payload['prices']['split,dividend'].get(symbol,[]))
        books[symbol]={d:{'raw':b,'split':b,'dividend':dividend.get(d),'valid':valid_bar(b) and d in sessions} for d,b in adjusted.items()}
        audit[symbol]={'missing_sessions':len(sessions-set(adjusted)), 'invalid_rows':sum(not b['valid'] for b in books[symbol].values()),
                       'large_adjusted_close_moves':sum(abs(adjusted[b]['c']/adjusted[a]['c']-1)>.5 for a,b in zip(sorted(adjusted),sorted(adjusted)[1:]) if adjusted[a]['c']>0)}
        # 大きな値動きは本物の可能性がある。成績を見て削除せず件数を報告する。
    return books,audit


def add_variants(scored,books,payload,days):
    raw={s:by_date(b) for s,b in payload['prices']['raw'].items()};index={d:i for i,d in enumerate(days)}
    for row in scored:
        s,d=row['symbol'],row['date'];i=index[d];book=books[s]
        previous=book.get(days[i-5]) if i>=5 else None
        change=book[d]['split']['c']/previous['split']['c']-1 if d in book and previous and previous['valid'] else None
        # 通知日までの20営業日の売買代金。未来の出来高は参照しない。
        window=days[max(0,i-19):i+1]
        amounts=[raw[s][x]['c']*raw[s][x]['v'] for x in window if x in raw[s] and valid_bar(raw[s][x])]
        liquid=median(amounts)>=1_000_000 if len(amounts)==20 else None
        base=row['repeated_up']
        row['repeated_up_capped']=False if base is False else None if base is None or change is None else change<=.10
        row['repeated_up_liquid']=False if base is False else None if base is None or liquid is None else liquid


def run(root,secret):
    out={'code_commit':os.environ.get('GITHUB_SHA','local'),'plan_sha256':digest(root/'research/history/PRACTICAL_PLAN.md'),
         'excluded_symbols':sorted(EXCLUDED),'reserved_2025_opened':False,'scope':'retrospectively_quality_selected_exploratory_subset',
         'provider_split_adjustment_accepted':True,'holding_sessions':10,'cost_each_side':.0025,'years':[]}
    private=[]
    for year in (2023,2024):
        p=load_expanded(root,year,secret);books,audit=prepare(p)
        scored,days=prepare_signals(p,books,[],year);add_variants(scored,books,p,days)
        summaries={};ledgers={};skips={}
        for model in (*MODELS,*VARIANTS):
            attempts,skip=schedule(scored,days,model,10,start=f'{year}-01-01',end=f'{year}-12-31')
            ledger=evaluate(attempts,books,days);summaries[model]=summarize(ledger);ledgers[model]=ledger;skips[model]=skip
        # 同じ親モデルの取引を採用/不採用へ分け、再購入日変更の影響と区別する。
        flags={(r['symbol'],r['date']):r for r in scored};paired={}
        for v in VARIANTS:
            parent=ledgers['repeated_up']
            paired[v]={label:summarize([t for t in parent if (flags[(t['symbol'],t['signal_date'])][v] is True)==keep]) for label,keep in [('kept',True),('removed',False)]}
        out['years'].append({'year':year,'included_symbols':len(books),'excluded_present':sorted(set(p['prices']['raw'])&EXCLUDED),
                            'quality':audit,'summaries':summaries,'parent_trade_filter_diagnostic':paired,'skips':skips})
        private.append({'year':year,'scored':scored,'ledgers':ledgers})
        print(json.dumps({'year':year,'included':len(books),'results':{m:{k:s[k] for k in ('priced','unresolved','mean_net_return','without_best_trade_mean')} for m,s in summaries.items()}}),flush=True)
    # 採用判定は探索期間2023年だけで固定。2024年は追認ではなく崩れるかを確認する。
    a=out['years'][0]['summaries'];eligible=[]
    for v in VARIANTS:
        s=a[v];b=a['repeated_up']
        if s['priced']>=30 and s['mean_net_return']>0 and s['mean_net_return']>b['mean_net_return'] and s['without_best_trade_mean']>0 and s['median_net_return']>b['median_net_return']:eligible.append(v)
    selected=max(eligible,key=lambda v:a[v]['mean_net_return']) if eligible else None
    out['selected_using_2023_only']=selected
    out['trial_count_new_variants']=2
    if selected:
        b=out['years'][1]['summaries'];s=b[selected]
        out['replicated_in_2024']=s['priced']>=30 and s['mean_net_return']>0 and s['mean_net_return']>b['repeated_up']['mean_net_return'] and s['without_best_trade_mean']>0 and s['median_net_return']>b['repeated_up']['median_net_return']
    else:out['replicated_in_2024']=False
    raw=json.dumps(private,sort_keys=True,allow_nan=False).encode();cipher=encrypt_bytes(raw,secret)
    assert decrypt_bytes(cipher,secret)==raw
    target=root/'data/history/encrypted/practical-ledgers.enc';target.write_bytes(cipher)
    out['encrypted_sha256']=digest(target)
    write_json(root/'diagnostics/history/practical/results.json',out)
    return out

if __name__=='__main__':run(ROOT,os.environ.get('ALPACA_SECRET_KEY',''))
