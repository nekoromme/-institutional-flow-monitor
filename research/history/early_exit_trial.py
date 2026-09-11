"""購入日に続伸できなかった場合、翌営業日始値で撤退する固定案。

通知当日の値で購入を選び、購入日の終値で撤退を判断する。終値を見てから
その終値で売れたとは仮定しない。再購入日は元の10日保有予定まで予約を
維持し、早く売ったことによる追加取引を混ぜずに撤退ルールを比較する。
"""
import json,os
from statistics import mean
from flow_probe.bulk13f import digest
from flow_probe.returns_core import evaluate,schedule,summarize,after_cost,COSTS
from .collect import ROOT,write_json,encrypt_bytes,decrypt_bytes
from .gap_audit import load_expanded
from .practical_validation import prepare
from .development_validation import prepare_signals


def exit_variant(t,books,days):
    if t['status']!='priced':return dict(t)
    b=books[t['symbol']];d=t['entry_date'];buy=b[d]['split']['o'];close=b[d]['split']['c']
    if close>=buy:return {**t,'early_exit':False}
    nxt=days[days.index(d)+1];sell=b[nxt]['split']['o'];ratio=sell/buy
    # 売却日のその後の高安を損益や逆行幅へ入れない。
    out={**t,'exit_date':nxt,'early_exit':True,'exit_execution':'next_session_open_after_entry_close_decision',
         'price_return':ratio-1,'net_return':after_cost(ratio,.0025),
         'cost_returns':{str(c):after_cost(ratio,c) for c in COSTS},
         'max_adverse_price_return':min(0,b[d]['split']['l']/buy-1,ratio-1),
         'net_excess_over_benchmark':None,'dividend_adjusted_reference_net':None,
         'dividend_adjusted_reference_return':None,'holding_sessions':2,'reserved_through':t['exit_date']}
    return out


def run(root,secret):
    out={'code_commit':os.environ.get('GITHUB_SHA'),'reserved_2025_opened':False,'plan_sha256':digest(root/'research/history/EARLY_EXIT_PLAN.md'),
         'new_rule_count':1,'cumulative_new_variants_including_horizons':5,'years':[]};private=[]
    for year in (2023,2024):
        p=load_expanded(root,year,secret);books,_=prepare(p);scored,days=prepare_signals(p,books,[],year)
        attempts,_=schedule(scored,days,'repeated_up',10,start=f'{year}-01-01',end=f'{year}-12-31')
        base=evaluate(attempts,books,days);variant=[exit_variant(t,books,days) for t in base]
        pairs=[(a,b) for a,b in zip(base,variant) if a['status']=='priced' and b.get('early_exit')]
        out['years'].append({'year':year,'base':summarize(base),'variant':summarize(variant),'triggered':len(pairs),
            'triggered_base':summarize([a for a,b in pairs]),'triggered_variant':summarize([b for a,b in pairs]),
            'mean_paired_change':mean(b['net_return']-a['net_return'] for a,b in pairs) if pairs else None,
            'helped_trades':sum(b['net_return']>a['net_return'] for a,b in pairs),
            'harmed_trades':sum(b['net_return']<a['net_return'] for a,b in pairs),
            'original_winners_exited_early':sum(a['net_return']>0 for a,b in pairs)})
        private.append({'year':year,'base':base,'variant':variant})
    def qualifies(y):
        s,b=y['variant'],y['base']
        return s['priced']>=30 and s['mean_net_return']>0 and s['mean_net_return']>b['mean_net_return'] and s['without_best_trade_mean']>0 and s['median_net_return']>b['median_net_return']
    out['passes_2023']=qualifies(out['years'][0]);out['passes_2024']=qualifies(out['years'][1]);out['adopt']=out['passes_2023'] and out['passes_2024']
    raw=json.dumps(private,sort_keys=True,allow_nan=False).encode();enc=encrypt_bytes(raw,secret);assert decrypt_bytes(enc,secret)==raw
    path=root/'data/history/encrypted/early-exit-ledgers.enc';path.write_bytes(enc);out['encrypted_sha256']=digest(path)
    write_json(root/'diagnostics/history/early-exit/results.json',out)
if __name__=='__main__':run(ROOT,os.environ.get('ALPACA_SECRET_KEY',''))
