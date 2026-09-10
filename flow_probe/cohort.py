"""過去に公表済みだった報告から、調査する証券の候補一覧を作る。

ここで作る200件は、低保有認定済みの投資候補ではない。銘柄の対応・
保有の重複・流動性を確認する順番を固定するための、最初の調査バッチ。
未来の保有変化や値上がり、現在の上場銘柄一覧は抽出順へ使わない。
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

from .bulk13f import PARSER_VERSION, _table, digest, extract_archive, integer, merge_archives, select_filings
from .holdings import ARCHIVES, SECURITIES, TAGS, _get_file, aggregate_security, public_summary, select_denominator
from .holdings_reference import reviewed_denominator
from .http_client import SafeHttp

VERSION = 'historical-frame-0.1'
AS_OF, PERIOD = '2026-01-01', '2025-09-30'
SEED, BATCH_SIZE = 'institutional-flow-monitor/frame-2026Q1-v1', 200
PRIOR_ARCHIVE = '01sep2025-30nov2025_form13f.zip'
PRIOR_SHA = '7753ae28988c076baeb30a94656622ff0b3c5f55c90078d9d69fc14ba3d3b682'
LIST_SHA = '7393a3557aff878c6e0cb452424a45a5df0241a1faf8ab94d748388d1de792af'


def read_security_list(path):
    """公式の固定幅表を読む。オプション取引可の印と、株式種類を混同しない。"""
    result = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        cusip = line[:9]
        if len(cusip) != 9 or not cusip.isalnum() or cusip in result:
            raise ValueError('ambiguous_official_security_list')
        result[cusip] = {'cusip': cusip, 'issuer': line[10:40].strip(),
                         'class': line[40:67].strip(), 'status': line[67:70].strip()}
    return result


def frozen_states(filings, *, period=PERIOD, as_of=AS_OF):
    """同日提出分も除く。保有期末と提出日の両方を時間の境界で確認する。"""
    if period >= as_of:
        raise ValueError('holdings_period_not_before_selection')
    states = select_filings(filings, period, as_of)
    return states, {
        'selected_period': period, 'as_of': as_of,
        'accepted_manager_states': sum(not s['issues'] for s in states.values()),
        'unresolved_manager_states': sum(bool(s['issues']) for s in states.values()),
        'same_period_filings_excluded_at_or_after_cutoff': sum(
            f['period'] == period and f['filed'] >= as_of for f in filings.values()),
        'later_period_filings_excluded': sum(f['period'] > period for f in filings.values()),
    }


def observed_row(row, official):
    """最初は普通株の狭い表記だけ。これでもファンド等の会社種類確認は必要。"""
    security = official.get(row['CUSIP'].strip())
    if not security or security['status'] == '*D*' or security['class'] not in {'COM', 'COM SHS'}:
        return False
    return (row['SSHPRNAMTTYPE'] == 'SH' and not row['PUTCALL'].strip()
            and (integer(row['SSHPRNAMT']) or 0) > 0
            and ' '.join(row['TITLEOFCLASS'].upper().split()) in {'COM', 'COM SHS', 'COMMON STOCK'})


def stable_batch(frame, *, size=BATCH_SIZE):
    """証券番号と固定の種だけで順番を作る。結果を見て抽出し直さない。"""
    if len({r['cusip'] for r in frame}) != len(frame):
        raise ValueError('duplicate_frame_security')
    ranked = [{**row, 'selection_hash': hashlib.sha256((SEED + '/' + row['cusip']).encode()).hexdigest()}
              for row in frame]
    ranked.sort(key=lambda row: (row['selection_hash'], row['cusip']))
    return ranked[:size]


def build_frame(paths, states, official):
    """大きい情報表を逐次読む。各運用者の当時の現行版だけを使う。"""
    active = {f['accession']: f for s in states.values() if not s['issues'] for f in s['filings']}
    observations = {}
    seen_archived_filings = set()
    scanned, accepted = 0, 0
    for path in paths:
        # 後年のアーカイブに当時の現行表がない場合は数百万行の走査を省く。
        with ZipFile(path) as archive:
            present = {r['ACCESSION_NUMBER'] for r in _table(archive, 'SUBMISSION', ('ACCESSION_NUMBER',))}
            usable = (present & active.keys()) - seen_archived_filings
            seen_archived_filings.update(usable)
            if not usable:
                continue
            for row in _table(archive, 'INFOTABLE', ('ACCESSION_NUMBER', 'CUSIP', 'SSHPRNAMTTYPE',
                                                   'SSHPRNAMT', 'PUTCALL', 'TITLEOFCLASS')):
                scanned += 1
                accession = row['ACCESSION_NUMBER']
                if accession not in usable or not observed_row(row, official):
                    continue
                accepted += 1
                cusip, filing = row['CUSIP'].strip(), active[accession]
                rec = observations.setdefault(cusip, {'managers': set(), 'filings': set(), 'filed': set()})
                rec['managers'].add(filing['cik'])
                rec['filings'].add(accession)
                rec['filed'].add(filing['filed'])
    if active.keys() - seen_archived_filings:
        raise ValueError('selected_filing_archive_missing')
    frame = []
    for cusip, obs in sorted(observations.items()):
        frame.append({**official[cusip], 'observed_reporting_managers': len(obs['managers']),
                      'observed_filings': len(obs['filings']), 'first_filed': min(obs['filed']),
                      'last_filed': max(obs['filed']), 'example_accession': min(obs['filings']),
                      'ticker_at_selection': None, 'low_ownership_eligible': None})
    ordinary = {c for c, r in official.items() if r['status'] != '*D*' and r['class'] in {'COM', 'COM SHS'}}
    return frame, {'information_rows_scanned_for_frame': scanned, 'accepted_observation_rows': accepted,
                   'official_narrow_common_securities': len(ordinary),
                   'securities_with_accepted_observation': len(frame),
                   'official_common_without_accepted_observation': len(ordinary - observations.keys()),
                   'unobserved_is_not_zero_ownership': True}


def pilot_snapshot(states, root, official):
    """指定5銘柄でも、将来公表の分母・保有数が混入しないことを実データで確認。"""
    results = {}
    for symbol, identity in SECURITIES.items():
        result = aggregate_security(states, identity['cusip'])
        result.pop('ledger')
        concepts = {}
        for taxonomy, tag in TAGS.items():
            path = root / f'data/sec-facts/{symbol}-{taxonomy}.json'
            concept = json.loads(path.read_text())
            if str(concept.get('cik')).zfill(10) != identity['cik'] or concept.get('tag') != tag:
                raise ValueError('historical_denominator_identity_mismatch')
            concept['source_url'] = f'https://data.sec.gov/api/xbrl/companyconcept/CIK{identity["cik"]}/{taxonomy}/{tag}.json'
            concepts[taxonomy] = concept
        result['denominator'] = reviewed_denominator(symbol, select_denominator(concepts, PERIOD, AS_OF))
        result['official_security'] = official.get(identity['cusip'])
        result['amended_managers_with_target_in_history'] = [
            {'cik': c, 'name': state['name'], 'operations': state['operations'], 'issues': state['issues']}
            for c, state in states.items() if identity['cusip'] in state['target_cusips_in_history']
            and any(o['operation'] != 'original' for o in state['operations'])]
        result['denominator_source_sha256'] = {t: digest(root / f'data/sec-facts/{symbol}-{t}.json') for t in TAGS}
        # 過去のCUSIPと現在の銘柄コードの結び付けは、ここでは既存5銘柄の点検用。
        # 広い候補一覧の正式な銘柄対応には流用しない。
        result['symbol_mapping_scope'] = 'existing_five_symbol_diagnostic_only'
        results[symbol] = result
    return public_summary({'as_of': AS_OF, 'quarters': {PERIOD: {'symbols': results}}})['quarters'][PERIOD]['symbols']


def run(root, *, fetch=False):
    folder = root / 'data/sec-bulk'
    client = SafeHttp(timeout=25, max_requests=30, max_seconds=900) if fetch else None
    sources = {PRIOR_ARCHIVE: PRIOR_SHA, **ARCHIVES}
    extracted, manifests = [], []
    for name, checksum in sources.items():
        path = folder / name
        if fetch:
            _get_file(client, path, 'https://www.sec.gov/files/structureddata/data/form-13f-data-sets/' + name,
                      checksum, max_bytes=125_000_000)
        if digest(path) != checksum:
            raise ValueError('historical_archive_changed')
        cache = path.with_suffix(path.suffix + '.extracted.json')
        data = json.loads(cache.read_text()) if cache.exists() else None
        wanted = sorted(i['cusip'] for i in SECURITIES.values())
        if not data or data['sha256'] != checksum or data['parser_version'] != PARSER_VERSION or data['target_cusips'] != wanted:
            data = extract_archive(path, set(wanted))
            cache.write_text(json.dumps(data))
        extracted.append(data)
        manifests.append({'name': name, 'sha256': checksum, 'bytes': path.stat().st_size,
                          'url': 'https://www.sec.gov/files/structureddata/data/form-13f-data-sets/' + name})
    filings = merge_archives(extracted)
    states, cutoff_audit = frozen_states(filings)
    list_path = folder / '13flist2025q3.txt'
    if fetch:
        _get_file(client, list_path, 'https://www.sec.gov/files/investment/13flist2025q3-txt.txt', LIST_SHA)
        for symbol, identity in SECURITIES.items():
            for taxonomy, tag in TAGS.items():
                _get_file(client, root / f'data/sec-facts/{symbol}-{taxonomy}.json',
                          f'https://data.sec.gov/api/xbrl/companyconcept/CIK{identity["cik"]}/{taxonomy}/{tag}.json')
    if digest(list_path) != LIST_SHA:
        raise ValueError('historical_security_list_changed')
    official = read_security_list(list_path)
    frame, coverage = build_frame([folder / name for name in sources], states, official)
    batch = stable_batch(frame)
    output = root / 'diagnostics/cohort'
    output.mkdir(parents=True, exist_ok=True)
    frame_path = output / 'full-frame.json'
    frame_path.write_text(json.dumps(frame, ensure_ascii=False, sort_keys=True, indent=2) + '\n')
    report = {
        'version': VERSION, 'generated_at': datetime.now(timezone.utc).isoformat(),
        'selection_as_of': AS_OF, 'holdings_period': PERIOD,
        'decision_policy': 'filing_date_strictly_before_2026-01-01; period_2025-09-30_only',
        'purpose': 'historical_security_review_frame_not_certified_low_ownership_universe',
        'archive_vintage': 'current_SEC_extracts_of_historical_filings_not_original_download_vintage',
        'archives': manifests, 'unique_filings': len(filings), 'cutoff_audit': cutoff_audit,
        'official_list': {'url': 'https://www.sec.gov/files/investment/13flist2025q3-txt.txt',
                          'sha256': digest(list_path), 'bytes': list_path.stat().st_size,
                          'historical_publication_timestamp_verified': False},
        'coverage': coverage, 'frame_sha256': digest(frame_path),
        'selection': {'seed': SEED, 'batch_size': BATCH_SIZE, 'hash': 'sha256(seed/cusip)',
                      'uses_current_ticker_list': False, 'uses_future_holdings_or_returns': False,
                      'role': 'first_review_batch; low_ownership_and_controls_not_assigned'},
        'review_batch': batch, 'pilot_selection_snapshot': pilot_snapshot(states, root, official),
        'certified_low_ownership_count': 0, 'matched_control_count': 0,
        'remaining_gates': ['historical_identity_listing_and_company_type',
                            'reported_ownership_coverage_and_overlap', 'historical_denominator_class',
                            'historical_size_liquidity_and_industry', 'comparison_protocol_and_sample_size'],
    }
    (output / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='過去時点の証券候補を再現する。機関流入の採点はしない。')
    parser.add_argument('--fetch', action='store_true', help='不足する公的資料だけ取得する。初回は合計約275MB。')
    args = parser.parse_args()
    result = run(Path(__file__).resolve().parents[1], fetch=args.fetch)
    print(json.dumps({'version': result['version'], 'coverage': result['coverage'],
                      'cutoff_audit': result['cutoff_audit'], 'review_batch_size': len(result['review_batch'])}, indent=2))
