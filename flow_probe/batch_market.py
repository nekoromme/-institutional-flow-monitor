"""固定54銘柄の日足を取得し、既存の出来高判定を一括実行する。

原価格・出来高は公開しない。一銘柄の不足や補正不一致を記録して、
残る銘柄は続ける。結果を見て境界値や銘柄群を変更する処理はない。
"""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from .__main__ import assert_no_secrets
from .alpaca import NY, fetch_pages, quality, split_adjustment_audit
from .batch_common import load_protocol, PROTOCOL_SHA
from .cjmb_pilot import validate_calendar
from .http_client import ProbeError, SafeHttp
from .pilot import score
from .research import prepare_daily


def quarter_signal(rows, field, expected_sessions):
    """不明日を『異常なし』にしない。同じ四半期の通知を一票にまとめる。"""
    if len(rows) != expected_sessions or any(r[field] is None for r in rows):
        return None
    return any(r[field] for r in rows)


def can_prepare_windows(audit):
    """対応が揃う有限の数値なら、不一致日の影響は各過去窓で判定できる。

    不一致を許容する関数ではない。prepare_dailyが当日と過去60日を確認し、
    不一致を含む窓を不明にする。古い1日の価格差で未来を永久停止しない。
    """
    return bool(audit.get('coverage_complete') and audit.get('invalid_pairs') == 0)


def run(root, client):
    protocol = load_protocol(root)
    # endは取得する足の範囲。提供元の補正を過去時点へ戻す指定ではない。
    # 品質照合は取得日、特徴量の株数単位は各判定日、と分けて扱う。
    retrieved_at = datetime.now(timezone.utc)
    adjustment_basis = retrieved_at.astimezone(NY).date().isoformat()
    sessions = client.json('https://paper-api.alpaca.markets/v2/calendar',
                           {'start': protocol['history_start'], 'end': protocol['evaluation_end']})
    days = validate_calendar(sessions)
    previous = root / 'data/stage2-daily.json'
    if previous.exists():
        prior_days = json.loads(previous.read_text())['session_days']
        if days != [d for d in prior_days if protocol['history_start'] <= d <= protocol['evaluation_end']]:
            raise ProbeError('batch_calendar_differs_from_existing_pilot')
    expected = sum(protocol['evaluation_start'] <= d <= protocol['evaluation_end'] for d in days)
    start = datetime.fromisoformat(protocol['history_start'] + 'T00:00:00').replace(tzinfo=NY)
    end = datetime.fromisoformat(protocol['evaluation_end'] + 'T23:59:59').replace(tzinfo=NY)
    output, private = [], {}
    folder = root / 'data/batch-market'; folder.mkdir(parents=True, exist_ok=True)
    for target in protocol['symbols']:
        symbol = target['symbol']; item = {'symbol': symbol, 'status': 'held', 'quarter_abnormal': None, 'quarter_repeated': None}
        try:
            raw, rm = fetch_pages(client, 'bars', (symbol,), start, end, feed='sip', timeframe='1Day', max_pages=2, adjustment='raw')
            adjusted, am = fetch_pages(client, 'bars', (symbol,), start, end, feed='sip', timeframe='1Day', max_pages=2, adjustment='split')
            private[symbol] = {'raw': raw, 'adjusted': adjusted}
            item['probes'] = [rm, am]
            q = quality(raw, 'bars', sessions, timeframe='1Day', complete=rm['complete'], start=start, end=end)[symbol]
            item['quality'] = q
            if not rm['complete'] or not am['complete']:
                item['reason'] = 'market_download_incomplete'
            elif any(q[k] for k in ('invalid_records','identical_rows_or_duplicate_bar_times','out_of_order','outside_requested_interval')):
                item['reason'] = 'invalid_market_records'
            else:
                audit = split_adjustment_audit(raw, adjusted, complete=True, through=adjustment_basis)
                # 補正差の原株数は公開しない。件数と該当日だけを記録する。
                item['adjustment_audit'] = {k:audit[symbol][k] for k in ('status','paired_days','unpaired_days','coverage_complete','invalid_pairs','price_mismatch_days','volume_mismatch_days','unresolved_days','zero_raw_volume_days')}
                if not can_prepare_windows(audit[symbol]):
                    item['reason'] = 'unreviewed_adjustment_difference_or_missing_pairs'
                else:
                    prepared, _ = prepare_daily(raw, sessions, audit)
                    scored = score(prepared, days)
                    private[symbol].update(prepared=prepared, scored=scored)
                    item.update(status='scored', calendar_sessions=expected,
                                abnormal_days=sum(r['abnormal'] is True for r in scored),
                                repeated_days=sum(r['repeated'] is True for r in scored),
                                unknown_abnormal_days=sum(r['abnormal'] is None for r in scored),
                                unknown_repeated_days=sum(r['repeated'] is None for r in scored),
                                unknown_abnormal_reasons=dict(Counter(r['reason'] for r in scored if r['abnormal'] is None)),
                                unknown_repeat_reasons=dict(Counter(r['repeat_reason'] for r in scored if r['repeated'] is None)),
                                quarter_abnormal=quarter_signal(scored, 'abnormal', expected),
                                quarter_repeated=quarter_signal(scored, 'repeated', expected),
                                scored_sha256=hashlib.sha256(json.dumps(scored, sort_keys=True, allow_nan=False).encode()).hexdigest())
        except (ProbeError, ValueError, KeyError, TypeError) as exc:
            item['reason'] = exc.summary()['category'] if isinstance(exc, ProbeError) else 'data_validation_' + type(exc).__name__
        output.append(item)
        if len(output) % 10 == 0:
            print(json.dumps({'batch_market_processed': len(output), 'total': len(protocol['symbols'])}), flush=True)
    body = json.dumps({'sessions':sessions,'symbols':private}, sort_keys=True, allow_nan=False).encode()
    (folder/'market-input-and-scores.json').write_bytes(body)
    return {'version':'batch-market-0.3','protocol_sha256':PROTOCOL_SHA,
            'retrieved_at_utc':retrieved_at.isoformat(),
            'provider_adjustment_audit_through':adjustment_basis,
            'feature_adjustment_basis':'each_decision_day_only',
            'unresolved_adjustment_policy':'hold_affected_input_windows; quarter_requires_all_days_known',
            'quality_repair_plan_sha256':hashlib.sha256((root/'docs/QUALITY_REPAIR_PLAN.md').read_bytes()).hexdigest(),
            'code_commit':os.environ.get('GITHUB_SHA','local'),'rows':output,
            'market_input_and_scores_sha256':hashlib.sha256(body).hexdigest(),
            'persistence':'private_runner_local_only_not_uploaded; original_snapshot_expires_after_job',
            'summary':{'requested_symbols':len(output),'scored_symbols':sum(r['status']=='scored' for r in output),
                       'held_symbols':sum(r['status']=='held' for r in output),'evaluation_sessions':expected},
            'actual_historical_notification_time':None,'returns':None}


def main():
    root=Path(__file__).resolve().parents[1]
    key=os.environ.get('ALPACA_API_KEY','').strip();secret=os.environ.get('ALPACA_SECRET_KEY','').strip()
    client=SafeHttp(key,secret,timeout=25,max_requests=160,max_seconds=600)
    try:
        if not key or not secret: raise ProbeError('missing_github_secrets')
        report=run(root,client)
    except Exception as exc:
        report={'version':'batch-market-0.1','error':{'category':'unexpected_'+type(exc).__name__}}
    report['http']=client.metrics()
    text=json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False);assert_no_secrets(text,[key,secret])
    (root/'diagnostics').mkdir(exist_ok=True);(root/'diagnostics/batch-market.json').write_text(text+'\n')
    print('BATCH_MARKET_BEGIN');print(text);print('BATCH_MARKET_END')
    return 0 if report.get('summary',{}).get('scored_symbols',0)>0 else 2


if __name__=='__main__':sys.exit(main())
