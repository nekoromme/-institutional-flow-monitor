"""品質修正の効果と、変更を見送った条件の根拠を残す。

同じ原データで旧・新の検査を比べる。後の保有増加を見て銘柄を戻す
処理はない。原データを公開ログへ出さず、件数・判定・ハッシュを残す。
"""
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sys

from .__main__ import assert_no_secrets
from .alpaca import NY, fetch_pages, numeric, split_adjustment_audit, timestamp_ns
from .batch_common import load_protocol, PROTOCOL_SHA
from .batch_compare import compare, contrast
from .batch_market import quarter_signal
from .bulk13f import digest
from .http_client import SafeHttp, ProbeError
from .pilot import score
from .research import prepare_daily


def hash_object(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def zero_assessment(daily, minute, trades, probes, start, end):
    """無取引と断定しない。通常約定の正の数量なら、0との矛盾を強く示す。

    約定条件によって日足と分足への算入が異なるため、単純合計から日足を
    作り直さない。全市場の取引の完全性まで認定する関数ではない。
    """
    lo, hi = timestamp_ns(start.isoformat()), timestamp_ns(end.isoformat())
    records = daily + minute + trades
    in_range = all(lo <= timestamp_ns(r['t']) < hi for r in records)
    complete = all(p['complete'] for p in probes) and in_range
    positive_trades = [r for r in trades if numeric(r.get('s')) and r['s'] > 0]
    # @だけの通常約定は、公式説明では出来高へ算入される。
    regular = [r for r in positive_trades if r.get('c') == ['@']]
    positive_minutes = sum(numeric(r.get('v')) and r['v'] > 0 for r in minute)
    zeros = [r for r in daily if r.get('v') == 0]
    if not complete:
        conclusion = 'incomplete_or_out_of_range_keep_held'
    elif len(daily) != 1:
        conclusion = 'daily_bar_missing_or_duplicate_keep_held'
    elif not zeros:
        conclusion = 'zero_not_reproduced_requires_vintage_review'
    elif regular or positive_minutes:
        conclusion = 'zero_daily_volume_conflicts_with_positive_intraday_evidence'
    elif positive_trades:
        conclusion = 'positive_trades_exist_but_bar_eligibility_unresolved'
    else:
        conclusion = 'zero_bar_conflicts_with_documented_emission_rule_no_trade_not_certified'
    return {'conclusion': conclusion, 'complete': complete, 'all_records_in_requested_day': in_range,
            'daily_rows': len(daily), 'daily_zero_rows': len(zeros), 'minute_rows': len(minute),
            'positive_volume_minute_rows': positive_minutes, 'trade_rows': len(trades),
            'positive_size_trade_rows': len(positive_trades), 'regular_positive_trade_rows': len(regular),
            'zero_accepted_as_valid_observation': False, 'automatic_replacement_performed': False}


def probe_zero(client):
    """同じニューヨーク日付を再取得。翌日0時は除き、時間外も対象にする。"""
    start = datetime(2026, 1, 5, tzinfo=NY)
    end = start + timedelta(days=1)
    rows, probes = {}, []
    for name, kind, timeframe, pages in [('daily', 'bars', '1Day', 1),
                                        ('minute', 'bars', '1Min', 2),
                                        ('trades', 'trades', None, 20)]:
        data, meta = fetch_pages(client, kind, ('LSH',), start, end-timedelta(microseconds=1),
                                 feed='sip', timeframe=timeframe, adjustment='raw',
                                 limit=10000, max_pages=pages)
        rows[name] = data['LSH']; probes.append(meta)
    result = zero_assessment(rows['daily'], rows['minute'], rows['trades'], probes, start, end)
    result.update(symbol='LSH', date='2026-01-05', probes=probes,
                  source_sha256=hash_object(rows), scope='full_New_York_calendar_day',
                  source_document='https://docs.alpaca.markets/us/docs/market-data-faq#how-are-bars-aggregated')
    return result, rows


def repaired_comparisons(protocol, market, private, holdings):
    """対象復帰後の月別比較。旧49銘柄の比較とは分ける。"""
    h = {r['symbol']: r for r in holdings['rows']}
    targets = {r['symbol']: r for r in protocol['symbols']}
    complete = [r['symbol'] for r in market['rows']
                if r['quarter_abnormal'] is not None and r['quarter_repeated'] is not None]
    output = {}
    for end in ['2026-01-31', '2026-02-28', '2026-03-31']:
        records = []
        for symbol in complete:
            rows = [r for r in private['symbols'][symbol]['scored'] if r['date'] <= end]
            # 株式種類の照合が解決していない結果を、ここだけ採点に使わない。
            valid = h[symbol]['security_lists_match']
            records.append({'symbol':symbol, 'a':quarter_signal(rows, 'abnormal', len(rows)),
                            's':quarter_signal(rows, 'repeated', len(rows)),
                            'o':h[symbol]['observed_reported_increase'] if valid else None,
                            'm':h[symbol]['matched_manager_increase'] if valid else None})
        output[end] = {}
        for group in ['primary_low_reported_ratio', 'relative_lower_third', 'all']:
            chosen = records if group == 'all' else [r for r in records if targets[r['symbol']][group]]
            output[end][group] = {'n':len(chosen), 'any_abnormal':sum(r['a'] is True for r in chosen),
                                  'any_repeated':sum(r['s'] is True for r in chosen),
                                  'abnormal_contrast':contrast(chosen, 'a', 'o'),
                                  'repeated_contrast':contrast(chosen, 's', 'o'),
                                  'matched_repeated_contrast':contrast(chosen, 's', 'm')}
    return output


def run(root, client):
    protocol = load_protocol(root)
    p = root/'data/batch-market/market-input-and-scores.json'
    private = json.loads(p.read_text())
    market = json.loads((root/'diagnostics/batch-market.json').read_text())
    prior = json.loads((root/'docs/evidence/batch-market-2026-09-10.json').read_text())
    holdings = json.loads((root/'docs/evidence/batch-holdings-2026-09-10.json').read_text())
    if market['protocol_sha256'] != PROTOCOL_SHA or digest(p) != market['market_input_and_scores_sha256']:
        raise ValueError('quality_repair_input_mismatch')
    if market.get('version') != 'batch-market-0.2':
        raise ValueError('quality_repair_requires_new_audit_basis')
    old = {r['symbol']:r for r in prior['rows']}
    days = [s['date'] for s in private['sessions']]
    changed, reinstated, diagnostics = [], [], []
    for current in market['rows']:
        symbol = current['symbol']; previous = old[symbol]
        source = private['symbols'].get(symbol)
        if not source: continue
        # 同じ今回の取得値に「期間末までの補正だけ」という旧検査を当てる。
        legacy = split_adjustment_audit(source['raw'], source['adjusted'], complete=True,
                                        through=protocol['evaluation_end'])[symbol]
        matched = None
        if legacy['status'] == 'comparable_sample' and current['status'] == 'scored':
            prepared, _ = prepare_daily(source['raw'], private['sessions'], {symbol:legacy})
            matched = score(prepared, days) == source['scored']
            if not matched: changed.append(symbol)
        if previous['quarter_repeated'] is None and current['quarter_repeated'] is not None:
            reinstated.append(symbol)
        entry = {'symbol':symbol, 'previous_status':previous['status'], 'current_status':current['status'],
                 'legacy_audit_on_current_input':legacy['status'], 'old_new_scores_equal_on_same_input':matched,
                 'previous_snapshot_score_hash_equal':current.get('scored_sha256') == previous.get('scored_sha256')
                    if current.get('scored_sha256') and previous.get('scored_sha256') else None,
                 'previous_quarter_known':previous['quarter_repeated'] is not None,
                 'current_quarter_known':current['quarter_repeated'] is not None}
        if symbol in ['SMSI', 'GAME', 'KAPA']:
            revised = split_adjustment_audit(source['raw'], source['adjusted'], complete=True,
                          through=market['provider_adjustment_audit_through'])[symbol]
            entry['corrected_audit'] = {k:revised[k] for k in ['status','paired_days','price_mismatch_days',
                            'volume_mismatch_days','unresolved_days','mismatch_examples']}
        diagnostics.append(entry)
    # 処理修正だけで既存の判定が変わった場合、成功扱いせず調査へ戻す。
    if changed: raise ValueError('quality_repair_changed_existing_scores')
    zero, raw_zero = probe_zero(client)
    folder = root/'data/quality-repair'; folder.mkdir(parents=True, exist_ok=True)
    (folder/'lsh-intraday.json').write_text(json.dumps(raw_zero, sort_keys=True, allow_nan=False))
    comparison = compare(protocol, market, holdings)
    return {'version':'quality-repair-0.1','code_commit':os.environ.get('GITHUB_SHA','local'),
            'plan_sha256':digest(root/'docs/QUALITY_REPAIR_PLAN.md'), 'protocol_sha256':PROTOCOL_SHA,
            'private_input_sha256':digest(p), 'holdings_sha256':digest(root/'docs/evidence/batch-holdings-2026-09-10.json'),
            'provider_adjustment_audit_through':market['provider_adjustment_audit_through'],
            'decision_features_use_future_events':False, 'reinstated_complete_symbols':reinstated,
            'known_quarter_symbols_before':sum(r['quarter_repeated'] is not None for r in prior['rows']),
            'known_quarter_symbols_after':sum(r['quarter_repeated'] is not None for r in market['rows']),
            'same_input_existing_score_changes':changed, 'symbol_diagnostics':diagnostics,
            'zero_volume_verification':zero, 'repaired_quarter_comparison':comparison,
            'repaired_month_prefix_comparisons':repaired_comparisons(protocol, market, private, holdings),
            'decisions':{'audit_basis_fix':'adopted_as_unit_and_date_correction',
                         'zero_volume_acceptance':'not_adopted_without_validity_evidence',
                         'one_month_signal_filter':'not_adopted_requires_separate_period_validation',
                         'positive_price_filter':'not_adopted_buyer_identity_and_excluded_accumulation_unresolved'},
            'new_quarter_validated':False, 'institutional_detection_accuracy':None,
            'raw_market_persistence':'private_runner_local_only_expires_after_job'}


def main():
    root = Path(__file__).resolve().parents[1]
    key = os.environ.get('ALPACA_API_KEY','').strip(); secret = os.environ.get('ALPACA_SECRET_KEY','').strip()
    client = SafeHttp(key, secret, timeout=25, max_requests=30, max_seconds=180)
    try:
        if not key or not secret: raise ProbeError('missing_github_secrets')
        report = run(root, client)
    except Exception as exc:
        report = {'version':'quality-repair-0.1', 'error': {'category':'unexpected_'+type(exc).__name__}}
    report['http'] = client.metrics()
    text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    assert_no_secrets(text, [key,secret])
    (root/'diagnostics').mkdir(exist_ok=True)
    (root/'diagnostics/quality-repair.json').write_text(text+'\n')
    print('QUALITY_REPAIR_BEGIN'); print(text); print('QUALITY_REPAIR_END')
    return 2 if 'error' in report else 0


if __name__ == '__main__': sys.exit(main())
