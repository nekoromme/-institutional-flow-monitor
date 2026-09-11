"""上方向だけを制限し、下落中の異常出来高を除外せず検証する。"""
import json,os
from statistics import median
from flow_probe.bulk13f import digest
from flow_probe.returns_core import schedule,evaluate,summarize
from .collect import ROOT,write_json,encrypt_bytes,decrypt_bytes
from .gap_audit import load_expanded
from .practical_validation import prepare
from .development_validation import prepare_signals
from .quiet_trial import quiet_signals


def upside_allowed(bars,anchor):
    """候補期間直前の終値を基準に日中高値の上振れだけを制限する。"""
    return max(b['h'] for b in bars)<=anchor*1.05+1e-10


def add_flags(scored,books,days):
    index={d:i for i,d in enumerate(days)}
    for r in scored:
        r.update(no_up_control=None,downside_allowed=None,stabilized=None,falling_signal=None,added_by_new_rule=False,removed_by_new_rule=False)
        i=index[r['date']];book=books[r['symbol']]
        if i<84 or r['repeated'] is None:continue
        ds=days[i-84:i+1]
        if any(d not in book or not book[d]['valid'] for d in ds):continue
        old,before,recent=ds[:60],ds[60:80],ds[80:]
        typical=lambda seq:median((book[d]['split']['h']-book[d]['split']['l'])/book[d]['split']['c'] for d in seq)
        anchor=book[days[i-5]]['split']['c'];now=book[r['date']]['split']['c'];previous=book[days[i-1]]['split']['c']
        accepted=typical(before)<=typical(old) and upside_allowed([book[d]['split'] for d in recent],anchor)
        r['no_up_control']=accepted;r['downside_allowed']=accepted and r['repeated']
        # 親条件が弱いときの固定追加案。過去5日が下落でも通知日だけ止まれば拾う。
        r['stabilized']=r['downside_allowed'] and now>=previous
        r['falling_signal']=r['downside_allowed'] and now<previous
        r['added_by_new_rule']=r['downside_allowed'] and not r['quiet_now']
        r['removed_by_new_rule']=bool(r['quiet_now']) and not r['downside_allowed']
        r['five_day_down']=now<anchor;r['signal_day_down']=now<previous
    return scored


def passes(s,control):
    return s['priced']>=30 and s['mean_net_return']>0 and s['without_best_trade_mean']>0 and s['mean_net_return']>control['mean_net_return']


def run(root,secret):
    out={'code_commit':os.environ.get('GITHUB_SHA'),'plan_sha256':digest(root/'research/history/DOWNSIDE_PLAN.md'),'reserved_2025_opened':False,'years':[],'new_variants_maximum':2};private=[]
    for year in (2023,2024):
        p=load_expanded(root,year,secret);books,_=prepare(p);scored,days=prepare_signals(p,books,[],year);quiet_signals(scored,books,days);add_flags(scored,books,days)
        flags={(r['symbol'],r['date']):r for r in scored};results={};ledgers={}
        for model in ('quiet_now','no_up_control','downside_allowed'):
            a,_=schedule(scored,days,model,10,start=f'{year}-01-01',end=f'{year}-12-31');ledgers[model]=evaluate(a,books,days);results[model]=summarize(ledgers[model])
        parent=ledgers['downside_allowed'];slices={}
        for field in ('added_by_new_rule','five_day_down','signal_day_down'):
            slices[field]={str(flag):summarize([t for t in parent if flags[(t['symbol'],t['signal_date'])][field] is flag]) for flag in (True,False)}
        out['years'].append({'year':year,'summaries':results,'parent_slices':slices,'added_signal_days':sum(r['added_by_new_rule'] for r in scored),'removed_signal_days':sum(r['removed_by_new_rule'] for r in scored)})
        private.append({'year':year,'scored':scored,'ledgers':ledgers})
    out['parent_passes']=all(passes(y['summaries']['downside_allowed'],y['summaries']['no_up_control']) for y in out['years'])
    # 親条件が両年で合格しないときだけ、あらかじめ定めた改善案を実行。
    out['improvement_executed']=not out['parent_passes']
    if not out['parent_passes']:
        for public,item in zip(out['years'],private):
            year=item['year'];p=load_expanded(root,year,secret);books,_=prepare(p);days=[s['date'] for s in p['sessions']]
            a,_=schedule(item['scored'],days,'stabilized',10,start=f'{year}-01-01',end=f'{year}-12-31')
            ledger=evaluate(a,books,days);item['ledgers']['stabilized']=ledger;public['summaries']['stabilized']=summarize(ledger)
        out['improvement_passes']=all(passes(y['summaries']['stabilized'],y['summaries']['no_up_control']) and y['summaries']['stabilized']['mean_net_return']>y['summaries']['downside_allowed']['mean_net_return'] for y in out['years'])
    else:out['improvement_passes']=None
    if os.environ.get('FOLLOWUP_FALLING')=='1':
        out['followup_plan_sha256']=digest(root/'research/history/DOWNSIDE_FOLLOWUP.md')
        for public,item in zip(out['years'],private):
            year=item['year'];p=load_expanded(root,year,secret);books,_=prepare(p);days=[s['date'] for s in p['sessions']]
            a,_=schedule(item['scored'],days,'falling_signal',10,start=f'{year}-01-01',end=f'{year}-12-31')
            ledger=evaluate(a,books,days);item['ledgers']['falling_signal']=ledger;public['summaries']['falling_signal']=summarize(ledger)
        out['followup_passes']=all(passes(y['summaries']['falling_signal'],y['summaries']['no_up_control']) and y['summaries']['falling_signal']['mean_net_return']>y['summaries']['downside_allowed']['mean_net_return'] for y in out['years'])
        out['new_variants_maximum']=3
    raw=json.dumps(private,sort_keys=True,allow_nan=False).encode();enc=encrypt_bytes(raw,secret);assert decrypt_bytes(enc,secret)==raw
    path=root/'data/history/encrypted/downside-ledgers.enc';path.write_bytes(enc);out['encrypted_sha256']=digest(path)
    write_json(root/'diagnostics/history/downside/results.json',out)
if __name__=='__main__':run(ROOT,os.environ.get('ALPACA_SECRET_KEY',''))
