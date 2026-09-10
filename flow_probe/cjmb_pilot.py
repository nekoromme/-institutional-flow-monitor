"""CJMBの日足を既存モデルへ渡す。価格・出来高の原数値は公開しない。

先に期間・ルール・対象を固定し、結果を見て境界値を調整しない。
同じ四半期の複数の異常日は、一つの四半期の保有比較に対応する。
それぞれを独立した的中・外れとして数える処理は含めない。
"""
from collections import Counter
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import sys

from .__main__ import assert_no_secrets
from .alpaca import NY, fetch_pages, quality, split_adjustment_audit
from .bulk13f import digest
from .http_client import ProbeError, SafeHttp
from .pilot import START, END, score
from .research import PROTOCOL, prepare_daily

VERSION = 'cjmb-volume-pilot-0.1'
HISTORY_START = '2025-09-01'
COMPARISON = 'docs/evidence/cjmb-comparison-2026-09-10.json'
COMPARISON_SHA = '51e15ca502cc591a4ea10f49ed9e6dfcf08708fb897b12fbed9de49ce6c187c7'


def validate_calendar(sessions):
    if not isinstance(sessions, list) or not sessions:
        raise ProbeError('invalid_cjmb_calendar')
    days = []
    for row in sessions:
        if not isinstance(row, dict):
            raise ProbeError('invalid_cjmb_calendar')
        day = row.get('date', '')
        date.fromisoformat(day)
        if not HISTORY_START <= day <= END or row.get('open') != '09:30' or row.get('close') not in {'13:00', '16:00'}:
            raise ProbeError('unreviewed_cjmb_session')
        days.append(day)
    if days != sorted(set(days)) or not any(START <= d <= END for d in days):
        raise ProbeError('invalid_cjmb_calendar_order_or_coverage')
    return days


def summarize_trial(scored, comparison):
    if (comparison['target'] != {'ticker': 'CJMB', 'cusip': '131100109', 'cik': '0002032545'}
            or comparison['periods'] != ['2025-12-31', END] or comparison['outcome_as_of'] != '2026-06-01'):
        raise ValueError('unreviewed_cjmb_holdings_comparison')
    if any(r['symbol'] != 'CJMB' or not START <= r['date'] <= END for r in scored):
        raise ValueError('scored_data_outside_fixed_cjmb_trial')
    judgments = [{k: r[k] for k in ('date', 'abnormal', 'repeated', 'reason', 'repeat_reason')} for r in scored]
    return {'purpose': 'engineering_trial_not_predictive_accuracy',
            'evaluation_period': {'start': START, 'end': END},
            'calendar_sessions': len(scored), 'abnormal_days': sum(r['abnormal'] is True for r in scored),
            'repeated_abnormal_days': sum(r['repeated'] is True for r in scored),
            'unknown_abnormal_days': sum(r['abnormal'] is None for r in scored),
            'unknown_repeat_days': sum(r['repeated'] is None for r in scored),
            'unknown_reasons': dict(Counter(r['reason'] for r in scored if r['abnormal'] is None)),
            'daily_judgments_without_market_values': judgments,
            'comparison_link': {'unit': 'one_symbol_calendar_quarter', 'linked_symbol_quarters': 1,
                'accuracy_eligible_symbol_quarters': 0,
                'reported_share_change': comparison['change']['diagnostic_reported_share_change'],
                'ratio_change_percentage_points': comparison['ratios']['ratio_change_percentage_points'],
                'is_actual_net_institutional_trade': False},
            'institutional_flow_label': None, 'accuracy': None, 'returns': None,
            'actual_historical_notification_time': None, 'filing_lead_days': None,
            'holdings_used_to_generate_signal': False,
            'limitations': ['one_security_one_quarter_is_not_validation_sample',
                           'low_ownership_threshold_and_control_group_not_fixed',
                           'holdings_have_unresolved_reference_and_confidential_status',
                           'historical_data_retrieved_now_not_archived_as_known_then',
                           'abnormal_volume_does_not_identify_buyer_or_trade_direction']}


def run(root, client):
    if digest(root / COMPARISON) != COMPARISON_SHA:
        raise ValueError('cjmb_comparison_input_changed')
    sessions = client.json('https://paper-api.alpaca.markets/v2/calendar', {'start': HISTORY_START, 'end': END})
    days = validate_calendar(sessions)
    # 既存5銘柄の試行が作った営業日と食い違う場合も、黙って進めない。
    previous_calendar = root / 'data/stage2-daily.json'
    if previous_calendar.exists():
        previous = json.loads(previous_calendar.read_text())
        expected = [d for d in previous['session_days'] if HISTORY_START <= d <= END]
        if days != expected:
            raise ProbeError('cjmb_calendar_differs_from_existing_pilot')
    start = datetime.fromisoformat(HISTORY_START + 'T00:00:00').replace(tzinfo=NY)
    end = datetime.fromisoformat(END + 'T23:59:59').replace(tzinfo=NY)
    raw, raw_meta = fetch_pages(client, 'bars', ('CJMB',), start, end, feed='sip',
                                timeframe='1Day', max_pages=5, adjustment='raw')
    adjusted, adjusted_meta = fetch_pages(client, 'bars', ('CJMB',), start, end, feed='sip',
                                          timeframe='1Day', max_pages=5, adjustment='split')
    checks = quality(raw, 'bars', sessions, timeframe='1Day', complete=raw_meta['complete'], start=start, end=end)
    audit = split_adjustment_audit(raw, adjusted, complete=raw_meta['complete'] and adjusted_meta['complete'], through=END)
    report = {'version': VERSION, 'protocol': PROTOCOL, 'commit': os.environ.get('GITHUB_SHA', 'local'),
              'history_start': HISTORY_START, 'history_end': END, 'probes': [raw_meta, adjusted_meta],
              'raw_data_quality': checks, 'adjustment_audit': audit,
              'comparison_sha256': COMPARISON_SHA,
              'data_persistence': 'raw_bars_prepared_windows_and_scores_local_only_not_uploaded'}
    folder = root / 'data/cjmb-pilot'; folder.mkdir(parents=True, exist_ok=True)
    raw_body = json.dumps({'raw': raw, 'adjusted': adjusted, 'sessions': sessions}, sort_keys=True, allow_nan=False).encode()
    (folder / 'market-input.json').write_bytes(raw_body)
    report['market_input_sha256'] = hashlib.sha256(raw_body).hexdigest()
    q = checks['CJMB']
    if (not raw_meta['complete'] or not adjusted_meta['complete']
            or any(q[k] for k in ('invalid_records', 'identical_rows_or_duplicate_bar_times', 'out_of_order', 'outside_requested_interval'))
            or audit['CJMB']['status'] != 'comparable_sample'):
        report['status'] = 'data_or_adjustment_review_required'
        return report
    prepared, preparation = prepare_daily(raw, sessions, audit)
    scored = score(prepared, days)
    if len(scored) != sum(START <= d <= END for d in days):
        raise ValueError('missing_cjmb_scored_sessions')
    for name, data in [('prepared.json', prepared), ('scored.json', scored)]:
        body = json.dumps(data, sort_keys=True, allow_nan=False).encode()
        (folder / name).write_bytes(body)
        report[name.removesuffix('.json') + '_sha256'] = hashlib.sha256(body).hexdigest()
    report['preparation'] = preparation
    report['trial'] = summarize_trial(scored, json.loads((root / COMPARISON).read_text()))
    report['status'] = 'completed' if any(r['abnormal'] is not None for r in scored) else 'no_evaluable_sessions'
    return report


def main():
    root = Path(__file__).resolve().parents[1]
    key = os.environ.get('ALPACA_API_KEY', '').strip()
    secret = os.environ.get('ALPACA_SECRET_KEY', '').strip()
    client = SafeHttp(key, secret, timeout=25, max_requests=25, max_seconds=240)
    try:
        if not key or not secret:
            raise ProbeError('missing_github_secrets')
        report = run(root, client)
    except ProbeError as exc:
        report = {'version': VERSION, 'status': 'blocked', 'error': exc.summary()}
    except Exception as exc:
        # 例外の本文や応答には原データが含まれ得るため、分類名だけを出す。
        report = {'version': VERSION, 'status': 'error', 'error': {'category': 'unexpected_' + type(exc).__name__}}
    report['http'] = client.metrics()
    text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    assert_no_secrets(text, [key, secret])
    (root / 'diagnostics').mkdir(exist_ok=True)
    (root / 'diagnostics/cjmb-pilot.json').write_text(text + '\n')
    print('CJMB_VOLUME_PILOT_BEGIN')
    print(text)
    print('CJMB_VOLUME_PILOT_END')
    return 0 if report['status'] == 'completed' else 2


if __name__ == '__main__':
    sys.exit(main())
