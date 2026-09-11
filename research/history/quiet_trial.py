"""事前の静けさ→異常出来高→価格停滞を順序どおりに判定する。"""
import json,os
from statistics import median
from collections import defaultdict
from flow_probe.bulk13f import digest
from flow_probe.returns_core import schedule,evaluate,summarize
from .collect import ROOT,write_json,encrypt_bytes,decrypt_bytes
from .gap_audit import load_expanded
from .practical_validation import prepare
from .development_validation import prepare_signals


def quiet_signals(scored,books,days):
    """直近5日を仕込み候補期間とし、それ以前の20日とさらに前の60日を比較。
    比較する全日は当日まで。終値だけ往復して静かに見える例を除くため、
    直近5日の日中高値〜安値の全範囲も5%以内に固定する。
    """
    index={d:i for i,d in enumerate(days)};grouped=defaultdict(list)
    for r in scored:grouped[r['symbol']].append(r)
    for s,rows in grouped.items():
        book=books[s];pending=None
        for r in sorted(rows,key=lambda x:x['date']):
            i=index[r['date']];r.update(calm_only=None,quiet_now=None,quiet_breakout=False)
            if i<84:continue
            all_days=days[i-84:i+1]
            if any(d not in book or not book[d]['valid'] for d in all_days):continue
            # 60+20+5営業日を重複なく分ける。
            old=all_days[:60];before=all_days[60:80];recent=all_days[80:]
            def typical(ds):return median((book[d]['split']['h']-book[d]['split']['l'])/book[d]['split']['c'] for d in ds)
            high=max(book[d]['split']['h'] for d in recent);low=min(book[d]['split']['l'] for d in recent)
            calm=typical(before)<=typical(old) and high/low-1<=.05
            r['calm_only']=calm if r['repeated'] is not None else None
            r['quiet_now']=calm and r['repeated'] if r['repeated'] is not None else None
            if pending:
                origin,ceiling=pending
                if i>origin+10:pending=None
                elif book[r['date']]['split']['c']>ceiling:
                    r['quiet_breakout']=True;pending=None
            # 最初の候補の上限を固定し、毎日更新して突破を後知恵で作らない。
            if pending is None and r['quiet_now'] and not r['quiet_breakout']:pending=(i,high)
    return scored


def run(root,secret):
    out={'code_commit':os.environ.get('GITHUB_SHA'),'plan_sha256':digest(root/'research/history/QUIET_PLAN.md'),
         'reserved_2025_opened':False,'years':[],'new_strategy_variants':2,'prior_variants':5};private=[];samples=[]
    for year in (2023,2024):
        p=load_expanded(root,year,secret);books,_=prepare(p);scored,days=prepare_signals(p,books,[],year)
        quiet_signals(scored,books,days);results={};ledgers={}
        for model in ('calm_only','quiet_now','quiet_breakout'):
            a,skip=schedule(scored,days,model,10,start=f'{year}-01-01',end=f'{year}-12-31')
            ledgers[model]=evaluate(a,books,days);results[model]=summarize(ledgers[model])
        # 負け/勝ちを見ずに、各年の最初の候補2銘柄を方向推定の取得対象にする。
        seen=set()
        for r in sorted(scored,key=lambda x:(x['date'],x['symbol'])):
            if r['quiet_now'] and r['symbol'] not in seen:
                samples.append({'year':year,'symbol':r['symbol'],'date':r['date']});seen.add(r['symbol'])
                if len(seen)==2:break
        out['years'].append({'year':year,'included_symbols':len(books),'signal_counts':{m:sum(r[m] is True for r in scored) for m in results},'summaries':results})
        private.append({'year':year,'scored':scored,'ledgers':ledgers})
    def passes(m):
        for y in out['years']:
            s=y['summaries'][m];control=y['summaries']['calm_only']
            if not(s['priced']>=30 and s['mean_net_return']>0 and s['without_best_trade_mean']>0 and s['mean_net_return']>control['mean_net_return']):return False
        return True
    out['passing_models']=[m for m in ('quiet_now','quiet_breakout') if passes(m)]
    out['direction_probe_required']=not bool(out['passing_models']);out['direction_samples']=samples
    raw=json.dumps(private,sort_keys=True,allow_nan=False).encode();cipher=encrypt_bytes(raw,secret);assert decrypt_bytes(cipher,secret)==raw
    dest=root/'data/history/encrypted/quiet-ledgers.enc';dest.write_bytes(cipher);out['encrypted_sha256']=digest(dest)
    write_json(root/'diagnostics/history/quiet/results.json',out)
    return out
if __name__=='__main__':run(ROOT,os.environ.get('ALPACA_SECRET_KEY',''))
