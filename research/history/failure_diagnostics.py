"""負け方を価格経路から分解する。企業ニュースの因果関係は断定しない。"""
import json,os
from statistics import mean,median
from collections import defaultdict
from flow_probe.bulk13f import digest
from flow_probe.returns_core import evaluate,schedule,summarize,after_cost
from .collect import ROOT,write_json,encrypt_bytes,decrypt_bytes
from .gap_audit import load_expanded
from .practical_validation import prepare,add_variants
from .development_validation import prepare_signals


def describe(t,books,days):
    """将来の値は負け方の事後診断だけに使い、購入判定へ戻さない。"""
    if t['status']!='priced':return t
    b=books[t['symbol']];i=days.index(t['entry_date']);j=days.index(t['exit_date']);buy=b[days[i]]['split']['o']
    closes=[after_cost(b[d]['split']['c']/buy,.0025) for d in days[i:j+1]]
    intraday=[after_cost(b[d]['split']['h']/buy,.0025) for d in days[i:j+1]]
    r={**t,'first_day_net':closes[0],'best_close_net':max(closes),'best_intraday_net':max(intraday),
       'close_net_path':closes,'net_peak_to_exit':max(closes)-closes[-1]}
    if t['net_return']>=0:r['failure_shape']='non_loss'
    elif t['price_return']>0:r['failure_shape']='cost_erased_gain'
    elif max(closes)>0:r['failure_shape']='profitable_close_then_loss'
    elif max(intraday)>0:r['failure_shape']='intraday_gain_only_then_loss'
    else:r['failure_shape']='never_recovered_cost_even_intraday'
    return r


def run(root,secret):
    out={'code_commit':os.environ.get('GITHUB_SHA'),'reserved_2025_opened':False,'diagnostic_only':True,
         'causation_established':False,'plan_sha256':digest(root/'research/history/FAILURE_PLAN.md'),'years':[]};private=[]
    for year in (2023,2024):
        p=load_expanded(root,year,secret);books,q=prepare(p);scored,days=prepare_signals(p,books,[],year);add_variants(scored,books,p,days)
        attempts,_=schedule(scored,days,'repeated_up',10,start=f'{year}-01-01',end=f'{year}-12-31')
        rows=[describe(t,books,days) for t in evaluate(attempts,books,days)]
        grouped=defaultdict(list)
        for t in rows:grouped[t.get('failure_shape','unresolved')].append(t)
        shapes={k:summarize(v) for k,v in grouped.items()}
        slices={}
        for field,cut in [('entry_gap',0),('pre_signal_five_day_return',.10),('signal_close_position',.5)]:
            slices[field]={name:summarize([t for t in rows if t.get(field) is not None and ((t[field]>cut)==above)]) for name,above in [('above',True),('at_or_below',False)]}
        # 同じ購入日を固定した5/10/20日経路と、各保有日数で再購入を組み直した取引を区別する。
        horizons={}
        for h in (5,10,20):
            changed=[]
            for t in attempts:
                j=days.index(t['signal_date'])+h
                changed.append({**t,'holding_sessions':h,'exit_date':days[j] if j<len(days) else None})
            paired=evaluate(changed,books,days)
            fresh,_=schedule(scored,days,'repeated_up',h,start=f'{year}-01-01',end=f'{year}-12-31')
            horizons[str(h)]={'fixed_entries':summarize(paired),'rescheduled':summarize(evaluate(fresh,books,days))}
        losses=[t for t in rows if t['status']=='priced' and t['net_return']<0]
        out['years'].append({'year':year,'summary':summarize(rows),'failure_shapes':shapes,'slices':slices,'horizons':horizons,
            'losing_trades':len(losses),'losses_with_negative_first_day':sum(t['first_day_net']<0 for t in losses),
            'worst_trades':[{k:t.get(k) for k in ('symbol','signal_date','net_return','failure_shape','entry_gap','pre_signal_five_day_return','first_day_net','best_close_net')} for t in sorted(losses,key=lambda t:t['net_return'])[:8]]})
        private.append({'year':year,'rows':rows})
    raw=json.dumps(private,allow_nan=False,sort_keys=True).encode();enc=encrypt_bytes(raw,secret);assert decrypt_bytes(enc,secret)==raw
    path=root/'data/history/encrypted/failure-ledgers.enc';path.write_bytes(enc);out['encrypted_sha256']=digest(path)
    write_json(root/'diagnostics/history/failure/results.json',out)
    print(json.dumps({'years':[{'year':y['year'],'losses':y['losing_trades'],'shapes':{k:v['priced'] for k,v in y['failure_shapes'].items()}} for y in out['years']]}))
if __name__=='__main__':run(ROOT,os.environ.get('ALPACA_SECRET_KEY',''))
