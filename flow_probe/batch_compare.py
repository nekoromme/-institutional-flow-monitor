"""事前固定した群の中で、異常の有無と公開保有報告の増加を比較する。

銘柄日数を標本数にしない。四半期ごと一銘柄一票。不明を陰性や
保有ゼロに変えず、比較相手がゼロ件なら差も計算しない。
"""
import json
from pathlib import Path
from .batch_common import load_protocol, PROTOCOL_SHA
from .bulk13f import digest


def contrast(rows, signal, outcome):
    groups = {}
    for value, name in [(True,'signal'),(False,'no_signal')]:
        selected = [r for r in rows if r[signal] is value and r[outcome] is not None]
        n = len(selected); successes = sum(r[outcome] is True for r in selected)
        groups[name] = {'symbols': [r['symbol'] for r in selected], 'n': n, 'reported_increases': successes,
                        'increase_fraction': successes/n if n else None}
    a,b = groups['signal'],groups['no_signal']
    return {**groups,'difference_percentage_points':100*(a['increase_fraction']-b['increase_fraction']) if a['n'] and b['n'] else None,
            'unit':'symbol_quarter','uncertainty':'descriptive_only; one_quarter_cross_stock_dependence_not_estimated'}


def compare(protocol, market, holdings):
    if market['protocol_sha256'] != PROTOCOL_SHA or holdings['protocol_sha256'] != PROTOCOL_SHA:
        raise ValueError('comparison_protocol_mismatch')
    expected = {r['symbol'] for r in protocol['symbols']}
    for data in [market,holdings]:
        names=[r['symbol'] for r in data['rows']]
        if set(names)!=expected or len(names)!=len(expected):raise ValueError('comparison_symbol_scope_mismatch')
    m={r['symbol']:r for r in market['rows']};h={r['symbol']:r for r in holdings['rows']};rows=[]
    for target in protocol['symbols']:
        symbol=target['symbol'];a,b=m[symbol],h[symbol];reasons=[]
        if a['quarter_abnormal'] is None:reasons.append('quarter_abnormal_unknown')
        if a['quarter_repeated'] is None:reasons.append('quarter_repeated_unknown')
        if not b['security_lists_match']:reasons.append('outcome_security_identity_unresolved')
        if b['observed_reported_increase'] is None:reasons.append('holdings_unobserved_in_one_or_both_quarters')
        rows.append({'symbol':symbol,'primary_low_reported_ratio':target['primary_low_reported_ratio'],
                     'relative_lower_third':target['relative_lower_third'],
                     'quarter_abnormal':a['quarter_abnormal'],'quarter_repeated':a['quarter_repeated'],
                     'observed_reported_increase':b['observed_reported_increase'] if b['security_lists_match'] else None,
                     'matched_manager_increase':b['matched_manager_increase'] if b['security_lists_match'] else None,
                     'strict_quality_clear':b['strict_quality_clear'],'remaining_reasons':reasons,
                     'market_reason':a.get('reason'),'actual_institutional_net_buy':None})
    groups={}
    for group in protocol['groups']:
        selected=rows if group=='all_eligible_descriptive_only' else [r for r in rows if r[group]]
        results={}
        for label, subset in [('observed_public_reports',selected),('strict_quality_sensitivity',[r for r in selected if r['strict_quality_clear']])]:
            results[label]={s:contrast(subset,s,'observed_reported_increase') for s in ['quarter_abnormal','quarter_repeated']}
        results['matched_managers_sensitivity']={s:contrast(selected,s,'matched_manager_increase') for s in ['quarter_abnormal','quarter_repeated']}
        groups[group]={'candidate_symbols':len(selected),'results':results}
    return {'version':'batch-comparison-0.1','protocol_sha256':PROTOCOL_SHA,'groups':groups,'rows':rows,
            'institutional_detection_accuracy':None,'investment_returns':None,'filing_lead_days':None,
            'limitations':['exploratory_single_calendar_quarter_not_independent_holdout',
              'unresolved_holdings_quality; observed_sum_not_complete_institutional_ownership',
              'quarter_end_positions_do_not_identify_intraperiod_trade_direction_or_timing',
              'selection_uses_reported_ratio_proxy_and_pre_cutoff_listing_proxy',
              'market_original_snapshot_not_persisted_privately',
              'no_multiple_quarter_or_market_sector_matched_validation']}


def run(root):
    p=root/'docs/evidence/batch-market-2026-09-10.json';q=root/'docs/evidence/batch-holdings-2026-09-10.json'
    result=compare(load_protocol(root),json.loads(p.read_text()),json.loads(q.read_text()))
    result['source_hashes']={'market':digest(p),'holdings':digest(q)}
    out=root/'diagnostics/batch/comparison.json';out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');return result


if __name__=='__main__':
    report=run(Path(__file__).resolve().parents[1]);print(json.dumps(report['groups'],indent=2))
