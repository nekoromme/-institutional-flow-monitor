"""モデルを変更せず、選別・方向・採点・データ保留への影響を検査する。

補正を未確認のまま外す計算やゼロ許容は、明記した実験専用の分岐。
現行モデルの採用判定へ戻さず、生の価格・出来高も公開しない。
"""
from collections import Counter
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
from statistics import median
from .__main__ import assert_no_secrets
from .alpaca import NY
from .batch_common import load_protocol, PROTOCOL_SHA
from .batch_compare import contrast
from .batch_market import quarter_signal
from .bulk13f import digest


def laboratory_score(bars, sessions, *, allow_zero=False):
    """株数補正を推定適用しない実験用採点。元・補正後を別々に比較する。

取得できない日は不明。正しい0と仮定する実験でも欠損の0埋めはしない。
本番のprepare_daily/classifyは呼び替えず、変更せずに残しておく。
    """
    daily={}
    for bar in bars:
        day=datetime.fromisoformat(bar['t'].replace('Z','+00:00')).astimezone(NY).date().isoformat()
        if day in daily:raise ValueError('duplicate_laboratory_day')
        daily[day]=bar
    history=[];judgments=[];output=[]
    for session in sessions:
        day=session['date'];prior=history[-60:];normal=session['open']=='09:30' and session['close']=='16:00';abnormal=None
        if normal and len(prior)==60 and all(d in daily for d in prior+[day]):
            values=[daily[d].get('v') for d in prior+[day]]
            if all(isinstance(v,(float,int)) and not isinstance(v,bool) and math.isfinite(v) and (v>=0 if allow_zero else v>0) for v in values):
                threshold=sorted(values[:-1])[56]
                if threshold>0:abnormal=values[-1]>threshold
        judgments.append(abnormal)
        window=judgments[-5:];repeated=(abnormal and sum(x is True for x in window)>=2) if len(window)==5 and all(x is not None for x in window) else None
        if '2026-01-01'<=day<='2026-03-31':output.append({'date':day,'abnormal':abnormal,'repeated':repeated})
        if normal:history.append(day)
    return output


def constant_scale(raw, adjusted):
    a={r['t']:r for r in raw};b={r['t']:r for r in adjusted}
    if not a or a.keys()!=b.keys() or len(a)!=len(raw) or len(b)!=len(adjusted):return {'status':'unpaired_or_duplicate'}
    for t in a:
        if not all(isinstance(r.get(k),(int,float)) and math.isfinite(r[k]) and r[k]>0 for r in [a[t],b[t]] for k in ['o','h','l','c']):return {'status':'invalid_price'}
    factor=median(b[t]['c']/a[t]['c'] for t in a)
    price_ok=all(math.isclose(b[t][k],a[t][k]*factor,rel_tol=1e-8,abs_tol=0.0001*(factor+1)) for t in a for k in ['o','h','l','c'])
    volume_ok=all(math.isclose(b[t]['v'],a[t]['v']/factor,rel_tol=1e-6,abs_tol=1) for t in a)
    return {'status':'uniform_reciprocal_scale' if price_ok and volume_ok else 'nonuniform_or_inconsistent_scale',
            'pairs':len(a),'observed_price_multiplier':round(factor,8),'prices_match':price_ok,'volumes_match':volume_ok,
            'official_corporate_action_certified':False}


def leave_one_out(rows, signal='s', outcome='o'):
    baseline=contrast(rows,signal,outcome)['difference_percentage_points'];deltas=[]
    for i in range(len(rows)):
        v=contrast(rows[:i]+rows[i+1:],signal,outcome)['difference_percentage_points']
        if v is not None:deltas.append(v)
    return {'baseline_difference_points':baseline,'valid_deletions':len(deltas),
            'minimum_difference_points':min(deltas) if deltas else None,'maximum_difference_points':max(deltas) if deltas else None,
            'opposite_sign_deletions':sum(v*baseline<0 for v in deltas) if baseline is not None else None,
            'interpretation':'fragility_check_not_confidence_interval'}


def run(root):
    protocol=load_protocol(root);private_path=root/'data/batch-market/market-input-and-scores.json'
    private=json.loads(private_path.read_text());sessions=private['sessions'];days=[r['date'] for r in sessions]
    market=json.loads((root/'diagnostics/batch-market.json').read_text());holdings=json.loads((root/'docs/evidence/batch-holdings-2026-09-10.json').read_text())
    if holdings['protocol_sha256']!=PROTOCOL_SHA or market['protocol_sha256']!=PROTOCOL_SHA:raise ValueError('factor_protocol_mismatch')
    if digest(private_path)!=market['market_input_and_scores_sha256']:raise ValueError('factor_private_input_mismatch')
    target={r['symbol']:r for r in protocol['symbols']};h={r['symbol']:r for r in holdings['rows']}
    complete=[r['symbol'] for r in market['rows'] if r['quarter_abnormal'] is not None and r['quarter_repeated'] is not None]
    prefix={};direction={'abnormal':Counter(),'other':Counter()};flips=[]
    for symbol in complete:
        if h[symbol]['observed_reported_increase']!=h[symbol]['matched_manager_increase']:flips.append(symbol)
        source=private['symbols'][symbol];by_day={datetime.fromisoformat(b['t'].replace('Z','+00:00')).astimezone(NY).date().isoformat():b for b in source['raw'][symbol]}
        for row in source['scored']:
            previous=days[days.index(row['date'])-1];a=by_day[previous]['c'];b=by_day[row['date']]['c']
            direction['abnormal' if row['abnormal'] else 'other']['up' if b>a else 'down' if b<a else 'flat']+=1
    for end in ['2026-01-31','2026-02-28','2026-03-31']:
        records=[]
        for symbol in complete:
            scored=[r for r in private['symbols'][symbol]['scored'] if r['date']<=end]
            records.append({'symbol':symbol,'a':quarter_signal(scored,'abnormal',len(scored)),
                            's':quarter_signal(scored,'repeated',len(scored)),
                            'o':h[symbol]['observed_reported_increase'],'m':h[symbol]['matched_manager_increase']})
        prefix[end]={}
        for name in ['primary_low_reported_ratio','relative_lower_third','all']:
            selected=records if name=='all' else [r for r in records if target[r['symbol']][name]]
            prefix[end][name]={'n':len(selected),'any_abnormal':sum(r['a'] is True for r in selected),'any_repeated':sum(r['s'] is True for r in selected),
                              'abnormal_contrast':contrast(selected,'a','o'),'repeated_contrast':contrast(selected,'s','o'),
                              'matched_repeated_contrast':contrast(selected,'s','m'),'leave_one_out':leave_one_out(selected)}
    split=[]
    for symbol in ['SMSI','GAME','KAPA']:
        source=private['symbols'][symbol];raw=source['raw'][symbol];adj=source['adjusted'][symbol]
        a=laboratory_score(raw,sessions);b=laboratory_score(adj,sessions)
        split.append({'symbol':symbol,'scale':constant_scale(raw,adj),'judgments_identical':a==b,
                      'raw_known_days':sum(r['abnormal'] is not None for r in a),
                      'raw_abnormal_days':sum(r['abnormal'] is True for r in a),'raw_repeated_days':sum(r['repeated'] is True for r in a),
                      'production_eligibility_changed':False})
    source=private['symbols']['LSH'];a=laboratory_score(source['raw']['LSH'],sessions);b=laboratory_score(source['raw']['LSH'],sessions,allow_zero=True)
    zero={'symbol':'LSH','assumption':'reported_zero_is_valid_no_trade_not_missing_data',
          'baseline_known_days':sum(r['abnormal'] is not None for r in a),'hypothetical_known_days':sum(r['abnormal'] is not None for r in b),
          'hypothetical_abnormal_days':sum(r['abnormal'] is True for r in b),'hypothetical_repeated_days':sum(r['repeated'] is True for r in b),
          'changed_dates':[x['date'] for x,y in zip(a,b) if x!=y],'production_eligibility_changed':False}
    return {'version':'factor-diagnostics-0.1','protocol_sha256':PROTOCOL_SHA,
            'plan_sha256':digest(root/'docs/FACTOR_DIAGNOSTIC_PLAN.md'),'code_commit':os.environ.get('GITHUB_SHA','local'),
            'private_market_sha256':digest(private_path),'holdings_sha256':digest(root/'docs/evidence/batch-holdings-2026-09-10.json'),
            'same_complete_cohort':complete,'month_prefix_comparisons':prefix,'price_direction_counts':{k:dict(v) for k,v in direction.items()},
            'outcome_direction_disagreement':{'n':len(flips),'denominator':len(complete),'symbols':flips},
            'split_scale_diagnostics':split,'zero_volume_sensitivity':zero,
            'optimization_performed':False,'production_model_changed':False,'causal_effect_identified':False,
            'new_quarter_validated':False,'institutional_detection_accuracy':None}


def main():
    root=Path(__file__).resolve().parents[1];report=run(root);text=json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)
    assert_no_secrets(text,[os.environ.get('ALPACA_API_KEY',''),os.environ.get('ALPACA_SECRET_KEY','')])
    (root/'diagnostics/factor-diagnostics.json').write_text(text+'\n');print('FACTOR_DIAGNOSTICS_BEGIN');print(text);print('FACTOR_DIAGNOSTICS_END')


if __name__=='__main__':main()
