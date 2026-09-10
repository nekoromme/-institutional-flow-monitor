"""旧・新で同じ取引、なくなった取引、新たに生じた取引を分ける。

平均の改善を全部『負けを除去できた』と説明しないための点検。
公開済みの派生損益だけを使い、市場データや秘密設定は不要。
"""
import json
from pathlib import Path
from statistics import mean


def change_groups(period):
    old={(r['symbol'],r['signal_date']):r for r in period['trade_ledger_10']['repeated']}
    new={(r['symbol'],r['signal_date']):r for r in period['trade_ledger_10']['repeated_up']}
    common=old.keys()&new.keys()
    # 同じ取引の損益が変わった場合は、条件変更だけの比較ではないので停止する。
    for k in common:
        for field in ('entry_date','exit_date','status','net_return'):
            if old[k].get(field)!=new[k].get(field):raise ValueError('shared_trade_changed')
    output={}
    for name,keys,rows in [('shared',common,old),('old_only',old.keys()-new.keys(),old),
                          ('new_only',new.keys()-old.keys(),new)]:
        chosen=[rows[k] for k in sorted(keys)]
        values=[r['net_return'] for r in chosen if r['status']=='priced']
        output[name]={'n':len(chosen),'priced':len(values),'mean_net_return':mean(values) if values else None,
                      'win_fraction':sum(v>0 for v in values)/len(values) if values else None,
                      'trades':[{'symbol':r['symbol'],'signal_date':r['signal_date'],
                                 'net_return':r.get('net_return')} for r in chosen]}
    return output


def main():
    root=Path(__file__).resolve().parents[1]
    evidence=root/'docs/evidence/next-returns-2026-09-10.json'
    data=json.loads(evidence.read_text())
    result={phase:change_groups(data[phase]) for phase in ('exploration','validation')}
    result['interpretation']='descriptive_trade_set_change_not_causal_effect_or_portfolio_return'
    destination=root/'diagnostics/return-change-review.json'
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    print(json.dumps({phase:{k:{key:value for key,value in v.items() if key!='trades'}
                            for k,v in result[phase].items()} for phase in ('exploration','validation')},ensure_ascii=False))


if __name__=='__main__':main()
