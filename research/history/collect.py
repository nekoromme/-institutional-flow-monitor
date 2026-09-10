"""3年分の研究の土台を収集する。ログには価格や秘密を出さない。

証券候補一覧の作成と、既存銘柄での収集の試験は分けて記録する。
後者を過去の正式な対象一覧と取り違えないことが重要。
"""
import argparse
import base64
from collections import Counter, defaultdict
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import sys
from zipfile import ZipFile

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from flow_probe.alpaca import NY, SYMBOLS, fetch_pages, numeric
from flow_probe.bulk13f import _table, digest, extract_archive, integer, merge_archives
from flow_probe.cohort import frozen_states
from flow_probe.http_client import SafeHttp, ProbeError
from flow_probe.return_diagnostics import validate_price_calendar

ROOT = Path(__file__).resolve().parents[2]
SEC_BASE = 'https://www.sec.gov/files/structureddata/data/form-13f-data-sets/'
ARCHIVES = {
    2023: ('2022q3_form13f.zip', '2022q4_form13f.zip'),
    2024: ('2023q3_form13f.zip', '2023q4_form13f.zip'),
    2025: ('01sep2024-30nov2024_form13f.zip', '01dec2024-28feb2025_form13f.zip'),
}
SEED = 'institutional-flow-monitor/history-frame-v1'
MAGIC = b'IFM-HISTORY-1\n'


def write_json(path, obj):
    """途中で終了しても、完成済みファイルと混ざらないよう一時保存後に置換。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(obj, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n')
    temporary.replace(path)


def cipher(secret, salt):
    if len(secret) < 16:
        raise ValueError('secret_missing_or_too_short')
    # APIの認証用途とは異なる鍵を、用途名とランダムな塩を使って導く。
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
               info=b'institutional-flow-monitor/history-storage/v1').derive(secret.encode())
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_bytes(raw, secret):
    salt = os.urandom(16)
    return MAGIC + salt + cipher(secret, salt).encrypt(gzip.compress(raw, mtime=0))


def decrypt_bytes(envelope, secret):
    if not envelope.startswith(MAGIC):
        raise ValueError('unknown_storage_format')
    start = len(MAGIC)
    salt = envelope[start:start+16]
    return gzip.decompress(cipher(secret, salt).decrypt(envelope[start+16:]))


def fetch_file(client, path, url):
    """同じ作業場所で再開する場合、検証済みの取得済みファイルを使う。"""
    record_path = path.with_suffix(path.suffix + '.source.json')
    if path.exists() and record_path.exists():
        record = json.loads(record_path.read_text())
        if record['url'] != url or digest(path) != record['sha256']:
            raise ValueError('cached_source_changed')
        return record
    raw = client.read(url, max_bytes=125_000_000)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.part')
    temporary.write_bytes(raw)
    temporary.replace(path)
    record = {'url': url, 'sha256': digest(path), 'bytes': len(raw),
              'retrieved_at_utc': datetime.now(timezone.utc).isoformat()}
    write_json(record_path, record)
    return record


def stable_batch(frame, size=200):
    ranked = [{**r, 'selection_hash': hashlib.sha256((SEED+'/'+r['cusip']).encode()).hexdigest()}
              for r in frame]
    ranked.sort(key=lambda r: (r['selection_hash'], r['cusip']))
    return ranked[:size]


def build_reported_frame(paths, states):
    """当時の現行報告だけを走査。現在の銘柄一覧や後の利益を使わない。

    表記が普通株でも、ファンド・上場状況などの確認は別工程で必要。
    この段階では株数を足し合わせないので保有比率も計算しない。
    """
    active = {f['accession']: f for s in states.values() if not s['issues'] for f in s['filings']}
    seen, observations = set(), {}
    scanned = 0
    for path in paths:
        with ZipFile(path) as archive:
            present = {r['ACCESSION_NUMBER'] for r in _table(archive, 'SUBMISSION', ('ACCESSION_NUMBER',))}
            usable = (present & active.keys()) - seen
            seen.update(usable)
            if not usable:
                continue
            for row in _table(archive, 'INFOTABLE', ('ACCESSION_NUMBER', 'CUSIP', 'NAMEOFISSUER',
                    'TITLEOFCLASS', 'SSHPRNAMT', 'SSHPRNAMTTYPE', 'PUTCALL')):
                scanned += 1
                accession = row['ACCESSION_NUMBER']
                if accession not in usable:
                    continue
                label = ' '.join(row['TITLEOFCLASS'].upper().split())
                cusip = row['CUSIP'].strip()
                if (label not in {'COM', 'COM SHS', 'COMMON STOCK'} or row['SSHPRNAMTTYPE'] != 'SH'
                    or row['PUTCALL'].strip() or not (integer(row['SSHPRNAMT']) or 0) > 0
                    or len(cusip) != 9 or not cusip.isalnum()):
                    continue
                filing = active[accession]
                rec = observations.setdefault(cusip, {'names': set(), 'classes': set(), 'managers': set(),
                                                      'filed': set(), 'accessions': set()})
                rec['names'].add(row['NAMEOFISSUER']); rec['classes'].add(label)
                rec['managers'].add(filing['cik']); rec['filed'].add(filing['filed'])
                rec['accessions'].add(accession)
    if active.keys() - seen:
        raise ValueError('active_filing_archive_missing')
    frame = [{'cusip': c, 'reported_names': sorted(r['names']), 'reported_classes': sorted(r['classes']),
              'reporting_managers': len(r['managers']), 'first_filed': min(r['filed']),
              'last_filed': max(r['filed']), 'example_accession': min(r['accessions']),
              'ticker_at_selection': None, 'historical_listing_verified': False,
              'ordinary_company_verified': False, 'low_ownership_verified': False}
             for c, r in sorted(observations.items())]
    return frame, scanned


def collect_frame(root, year, client):
    paths, sources, parsed = [], [], []
    for name in ARCHIVES[year]:
        print(json.dumps({'progress': 'sec_archive', 'year': year, 'file': name}), flush=True)
        path = root/'data/history/sec'/name
        sources.append(fetch_file(client, path, SEC_BASE+name)); paths.append(path)
        parsed.append(extract_archive(path, set()))
    filings = merge_archives(parsed)
    cutoff, period = f'{year}-01-01', f'{year-1}-09-30'
    states, audit = frozen_states(filings, period=period, as_of=cutoff)
    frame, scanned = build_reported_frame(paths, states)
    if not frame or any(r['last_filed'] >= cutoff for r in frame):
        raise ValueError('empty_or_future_frame')
    destination = root/f'diagnostics/history/frames/{year}.json'
    write_json(destination, {'selection_as_of': cutoff, 'holdings_period': period,
                            'frame': frame, 'review_batch': stable_batch(frame), 'seed': SEED})
    report = {'year': year, 'selection_as_of': cutoff, 'holdings_period': period, 'sources': sources,
              'cutoff_audit': audit, 'frame_count': len(frame), 'review_batch_count': min(200,len(frame)),
              'information_rows_validated': sum(p['information_rows'] for p in parsed),
              'information_rows_scanned_for_frame': scanned, 'frame_sha256': digest(destination),
              'certified_investment_universe': False, 'current_ticker_list_used': False,
              'archive_vintage': 'current_extract_of_historical_filings',
              'historical_ticker_listing_and_ownership_mapping': 'pending'}
    write_json(root/f'diagnostics/history/frame-{year}-summary.json', report)
    return report


def bar_quality(groups, days):
    """未取得日を原因未確定のまま数える。上場前も欠落も0にしない。"""
    expected = set(days); result = {}
    for symbol, rows in groups.items():
        dates, invalid, zero = [], 0, 0
        for row in rows:
            try:
                day = datetime.fromisoformat(row['t'].replace('Z','+00:00')).astimezone(NY).date().isoformat()
                dates.append(day)
                if not all(numeric(row.get(k)) for k in ('o','h','l','c','v')):
                    raise ValueError('not_numeric')
                if not (0 < row['l'] <= min(row['o'],row['c']) <= max(row['o'],row['c']) <= row['h'] and row['v'] >= 0):
                    raise ValueError('invalid_price_or_volume')
                zero += row['v'] == 0
            except (KeyError, TypeError, ValueError):
                invalid += 1
        present = set(dates)
        result[symbol] = {'records': len(rows), 'expected_sessions': len(days),
                          'missing_sessions': len(expected-present), 'missing_dates': sorted(expected-present),
                          'non_session_dates': sorted(present-expected), 'duplicate_dates': len(dates)-len(present),
                          'ordered': dates == sorted(dates), 'invalid_rows': invalid, 'zero_volume_rows': zero,
                          'first_date': min(dates) if dates else None, 'last_date': max(dates) if dates else None}
    return result


def collect_market(root, client, secret):
    universe = json.loads((root/'docs/evidence/batch-comparison-protocol-2026-09-10.json').read_text())
    symbols = tuple(sorted({r['symbol'] for r in universe['symbols']} | set(SYMBOLS) | {'SPY'}))
    chunks = [('2022-baseline','2022-09-01','2022-12-31'),
              ('2023','2023-01-01','2023-12-31'), ('2024','2024-01-01','2024-12-31'),
              ('2025-reserved','2025-01-01','2025-12-31'), ('2026-exits','2026-01-01','2026-01-31')]
    reports = []
    for name, start_day, end_day in chunks:
        print(json.dumps({'progress':'market_chunk','chunk':name}), flush=True)
        sessions = client.json('https://paper-api.alpaca.markets/v2/calendar', {'start':start_day,'end':end_day})
        days = validate_price_calendar(sessions, start=start_day, end=end_day)
        data, probes, audits = {}, [], {}
        for adjustment in ('raw','split','split,dividend'):
            rows, meta = fetch_pages(client, 'bars', symbols,
                datetime.fromisoformat(start_day+'T00:00:00').replace(tzinfo=NY),
                datetime.fromisoformat(end_day+'T23:59:59').replace(tzinfo=NY),
                feed='sip', timeframe='1Day', adjustment=adjustment, limit=10000, max_pages=10)
            if not meta['complete']:
                raise ProbeError('historical_market_download_incomplete')
            data[adjustment] = rows; probes.append(meta); audits[adjustment] = bar_quality(rows, days)
        different = []
        for symbol in symbols:
            sets = [{r['t'] for r in data[a][symbol]} for a in data]
            if any(s != sets[0] for s in sets[1:]): different.append(symbol)
        payload = {'chunk':name, 'start':start_day, 'end':end_day, 'symbols':symbols,
                   'scope':'existing_symbols_collection_probe_not_historical_universe',
                   'sessions':sessions, 'prices':data, 'probes':probes,
                   'retrieved_at_utc':datetime.now(timezone.utc).isoformat()}
        raw = json.dumps(payload, sort_keys=True, allow_nan=False).encode()
        encrypted = encrypt_bytes(raw, secret)
        # 復号できない保存物を成功扱いしない。比較はメモリ内で行い公開しない。
        if decrypt_bytes(encrypted,secret) != raw:
            raise ValueError('encrypted_roundtrip_failed')
        out = root/f'data/history/encrypted/{name}.enc';out.parent.mkdir(parents=True,exist_ok=True)
        out.write_bytes(encrypted)
        report = {'chunk':name,'start':start_day,'end':end_day,'sessions':len(days),'symbols':len(symbols),
                  'probes':probes,'quality':audits,'adjustment_date_mismatch_symbols':different,
                  'plaintext_sha256':hashlib.sha256(raw).hexdigest(),'encrypted_sha256':digest(out),
                  'encrypted_bytes':out.stat().st_size,'decrypt_roundtrip_verified':True,
                  'signal_or_return_computed':False, 'split_and_dividend_events_verified':False,
                  'scope':'existing_symbols_collection_probe_not_historical_universe'}
        write_json(root/f'diagnostics/history/market-{name}-summary.json',report);reports.append(report)
    return reports


def main():
    parser = argparse.ArgumentParser(description='原データの収集だけを行う。損益は計算しない。')
    parser.add_argument('--scope', choices=('all','market','public'), default='all')
    args = parser.parse_args()
    key, secret = os.environ.get('ALPACA_API_KEY',''), os.environ.get('ALPACA_SECRET_KEY','')
    if args.scope != 'public' and (not key or len(secret)<16):
        print('history_credentials_missing');return 1
    market = SafeHttp(key,secret,timeout=30,max_requests=100,max_seconds=300)
    sec = SafeHttp(timeout=60,max_requests=20,max_seconds=720)
    result = {'version':'history-foundation-0.1','code_commit':os.environ.get('GITHUB_SHA','local'),
              'plan_sha256':digest(ROOT/'research/history/PLAN.md'), 'frames':[], 'market':[],
              'returns_computed':False,'historical_universe_ready':False,'errors':[], 'requested_scope':args.scope}
    # 市場と各年の公的資料は独立。片方の不足で他方の保存まで失わない。
    try:
        if args.scope != 'public':
            result['market'] = collect_market(ROOT,market,secret)
    except (ProbeError,ValueError,KeyError,TypeError) as exc:
        result['errors'].append({'stage':'market','error':exc.summary() if isinstance(exc,ProbeError) else type(exc).__name__})
    for year in (ARCHIVES if args.scope != 'market' else ()):
        try:
            result['frames'].append(collect_frame(ROOT,year,sec))
        except (ProbeError,ValueError,KeyError,TypeError) as exc:
            result['errors'].append({'stage':f'frame-{year}', 'error':exc.summary() if isinstance(exc,ProbeError) else type(exc).__name__})
    result['http'] = {'market':market.metrics(),'sec':sec.metrics()}
    result['collection_complete'] = (args.scope=='public' or len(result['market'])==5) and (args.scope=='market' or len(result['frames'])==3) and not result['errors']
    write_json(ROOT/'diagnostics/history/summary.json',result)
    # 全候補と品質の明細は添付へ。公開ログには件数・ハッシュだけ。
    compact = {**result,'market':[{k:v for k,v in m.items() if k!='quality'} for m in result['market']]}
    text = json.dumps(compact,ensure_ascii=False,allow_nan=False)
    if (key and key in text) or (secret and secret in text): raise ValueError('secret_in_public_summary')
    print('HISTORY_DATA_BEGIN'); print(text); print('HISTORY_DATA_END')
    return 0 if result['collection_complete'] else 1


if __name__ == '__main__':
    sys.exit(main())
