"""固定済みルールの技術試行。2026年Q1のみ。機関検出の成績は計算しない。"""
from collections import Counter, defaultdict
from datetime import date
import hashlib
import json
import math
import os
from pathlib import Path
from statistics import median

from .research import PROTOCOL

VERSION = 'volume-pilot-0.1'
START, END, PREVIOUS = '2026-01-01', '2026-03-31', '2025-12-31'


def positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def classify(row):
    if row['status'] != 'prepared':
        return {'abnormal': None, 'reason': row['status']}
    values = row.get('baseline_volumes_in_decision_day_shares', [])
    if len(values) != 60 or not all(positive(v) for v in values) or not positive(row.get('raw_volume')):
        return {'abnormal': None, 'reason': 'invalid_prepared_volume'}
    threshold = sorted(values)[56]  # nearest-rank ceil(60 * .95) = 57; equality is not abnormal.
    return {'abnormal': row['raw_volume'] > threshold, 'reason': None,
            'threshold_volume': threshold, 'volume_multiple': row['raw_volume'] / median(values)}


def score(records, session_days, *, start=START, end=END):
    if session_days != sorted(set(session_days)):
        raise ValueError('invalid_session_calendar')
    for day in session_days:
        date.fromisoformat(day)
    grouped = defaultdict(dict)
    for row in records:
        symbol, day = row['symbol'], row['date']
        if day in grouped[symbol] or day not in session_days:
            raise ValueError('duplicate_or_noncalendar_prepared_row')
        grouped[symbol][day] = row
    output = []
    for symbol, by_day in sorted(grouped.items()):
        # Four preceding sessions are needed for repetition. No later days are scored.
        selected = [i for i, d in enumerate(session_days) if start <= d <= end]
        if not selected:
            continue
        judgments = {}
        for i in range(max(0, selected[0] - 4), selected[-1] + 1):
            day = session_days[i]
            row = by_day.get(day)
            judgments[day] = classify(row) if row else {'abnormal': None, 'reason': 'missing_prepared_day'}
            if i not in selected:
                continue
            window = session_days[max(0, i-4):i+1]
            known = len(window) == 5 and all(judgments[d]['abnormal'] is not None for d in window)
            count = sum(judgments[d]['abnormal'] is True for d in window) if known else None
            repeat = judgments[day]['abnormal'] and count >= 2 if known else None
            output.append({'symbol': symbol, 'date': day, **judgments[day],
                           'abnormal_count_in_five_sessions': count, 'repeated': repeat,
                           'repeat_reason': None if known else 'incomplete_five_session_judgments'})
    return output


def summarize(scored, holdings):
    if holdings['as_of'] != '2026-06-01':
        raise ValueError('unreviewed_holdings_snapshot')
    grouped = defaultdict(list)
    for row in scored:
        if not START <= row['date'] <= END:
            raise ValueError('outside_fixed_pilot_period')
        grouped[row['symbol']].append(row)
    output = {}
    for symbol, rows in sorted(grouped.items()):
        a = holdings['quarters'][PREVIOUS]['symbols'][symbol]
        b = holdings['quarters'][END]['symbols'][symbol]
        reasons = Counter(r['reason'] for r in rows if r['abnormal'] is None)
        flags = ['historical_low_ownership_universe_not_established',
                 'aggregate_holdings_not_certified', 'quarter_does_not_identify_trade_day']
        if any(x['unresolved_manager_references_count'] or x['potential_overlap_relationships_count'] or
               x['repeated_row_payloads_count'] for x in (a, b)):
            flags.append('joint_reporting_or_repeated_rows_unresolved')
        if any(x['unresolved_filings_count'] or x['unreviewed_class_rows_count'] for x in (a, b)):
            flags.append('filings_or_security_class_unresolved')
        output[symbol] = {
            'calendar_sessions': len(rows), 'abnormal_days': sum(r['abnormal'] is True for r in rows),
            'repeated_abnormal_days': sum(r['repeated'] is True for r in rows),
            'unknown_abnormal_days': sum(r['abnormal'] is None for r in rows),
            'unknown_repeat_days': sum(r['repeated'] is None for r in rows),
            'unknown_abnormal_reasons': dict(reasons),
            'diagnostic_reported_shares_previous': a['reported_share_sum_before_overlap_resolution'],
            'diagnostic_reported_shares_end': b['reported_share_sum_before_overlap_resolution'],
            'diagnostic_reported_share_change': b['reported_share_sum_before_overlap_resolution'] - a['reported_share_sum_before_overlap_resolution'],
            'institutional_flow_label': None, 'low_ownership_at_signal': None,
            'eligible_for_accuracy_measurement': False, 'exclusion_reasons': flags,
        }
    return {'version': VERSION, 'protocol': PROTOCOL['version'],
            'evaluation_period': {'from': START, 'through': END, 'previous_holdings_period': PREVIOUS},
            'purpose': 'technical_trial_not_performance_validation',
            'market_data_vintage': 'current_historical_snapshot_not_archived_as_known_then',
            'holdings_snapshot_as_of': holdings['as_of'],
            'holdings_used_to_generate_signal': False, 'label_unit': 'symbol_calendar_quarter',
            'actual_historical_notification_time': None, 'filing_lead_days': None,
            'accuracy': None, 'return_results': None,
            'other_periods_scored': False, 'by_symbol': output}


def main():
    root = Path(__file__).resolve().parents[1]
    prepared_path = root / 'data/stage2-daily.json'
    holdings_path = root / 'docs/evidence/sec-bulk-holdings-reviewed-2026-09-10.json'
    body = prepared_path.read_bytes()
    data = json.loads(body)
    if data['protocol'] != PROTOCOL:
        raise ValueError('unreviewed_preparation_protocol')
    records = data['records']
    days = data['session_days']
    # prepare_daily emits every calendar session for every symbol, including unknown days.
    # Missing rows for one symbol remain unknown, never compressed out of the repeat window.
    scored = score(records, days)
    report = summarize(scored, json.loads(holdings_path.read_text()))
    report['commit'] = os.environ.get('GITHUB_SHA', 'local')
    report['source_sha256'] = {'prepared': hashlib.sha256(body).hexdigest(),
                               'holdings': hashlib.sha256(holdings_path.read_bytes()).hexdigest()}
    raw = json.dumps(scored, sort_keys=True, allow_nan=False).encode()
    (root / 'data/pilot-scored.json').write_bytes(raw)
    report['scored_dataset_sha256'] = hashlib.sha256(raw).hexdigest()
    report['persistence'] = 'raw_inputs_and_daily_scores_not_uploaded; runner_files_expire'
    text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    (root / 'diagnostics').mkdir(exist_ok=True)
    (root / 'diagnostics/pilot-summary.json').write_text(text + '\n')
    print('VOLUME_PILOT_BEGIN')
    print(text)
    print('VOLUME_PILOT_END')


if __name__ == '__main__':
    main()
