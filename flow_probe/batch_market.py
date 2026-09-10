"""固定54銘柄の日足を取得し、既存の出来高判定を一括実行する。

原価格・出来高は公開しない。一銘柄の不足や補正不一致を記録して、
残る銘柄は続ける。結果を見て境界値や銘柄群を変更する処理はない。
"""
from datetime import datetime
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


def run(root, client):
    protocol = load_protocol(root)
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
                audit = split_adjustment_audit(raw, adjusted, complete=True, through=protocol['evaluation_end'])
                # 補正差の原株数は公開しない。件数と該当日だけを記録する。
                item['adjustment_audit'] = {k:audit[symbol][k] for k in ('status','paired_days','unpaired_days','coverage_complete','invalid_pairs','price_mismatch_days','volume_mismatch_days','unresolved_days')}
                if audit[symbol]['status'] != 'comparable_sample':
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
    return {'version':'batch-market-0.1','protocol_sha256':PROTOCOL_SHA,
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
