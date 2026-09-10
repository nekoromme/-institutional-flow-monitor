"""CJMBの後の二期を比較し、答え合わせの材料と欠損の影響を記録する。

2026年初の対象選びと、6月初までの報告による答え合わせは別の処理。
後から判明した保有や株数を、過去の対象選び・出来高判定へ渡さない。
保有が報告されないことをゼロ保有や売却と決めつける処理も含めない。
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import re
from zipfile import ZipFile

from .admission_review import same_size_positions
from .bulk13f import PARSER_VERSION, _table, digest, extract_archive, merge_archives, select_filings
from .eligibility import quality_summary
from .filing_review import apply_filing_reviews, canonical_row, compare_tables, object_hash, tsv_row, xml_rows
from .holdings import ARCHIVES, LISTS, _get_file, aggregate_security, match_security_list
from .http_client import SafeHttp
from .identity_sources import fields, parse_cover, source_documents, xml_primary
from .share_facts import outstanding_candidates, parse_document, split_tag

VERSION = 'cjmb-comparison-0.1'
CATALOG_PATH = 'docs/evidence/cjmb-comparison-catalog-2026-09-10.json'
ADMISSION_PATH = 'docs/evidence/admission-review-2026-09-10.json'
CIK, CUSIP = '0002032545', '131100109'
START_PERIOD, END_PERIOD = '2025-12-31', '2026-03-31'
SELECTION_AS_OF, OUTCOME_AS_OF = '2026-01-01', '2026-06-01'


def load_archives(root, client):
    extracted, sources = [], []
    folder = root / 'data/sec-cjmb'; folder.mkdir(parents=True, exist_ok=True)
    for name, expected in ARCHIVES.items():
        path = root / 'data/sec-bulk' / name
        if not path.exists() and client is None:
            raise ValueError('missing_archive_use_fetch')
        meta = _get_file(client, path, 'https://www.sec.gov/files/structureddata/data/form-13f-data-sets/' + name,
                         expected, max_bytes=125_000_000)
        cache = folder / (name + '.extracted.json')
        checksum = cache.with_suffix(cache.suffix + '.sha256')
        if cache.exists():
            if not checksum.exists() or digest(cache) != checksum.read_text().strip():
                raise ValueError('comparison_cache_changed')
            data = json.loads(cache.read_text())
            if data['target_cusips'] != [CUSIP] or data['sha256'] != expected:
                raise ValueError('comparison_cache_scope_changed')
        else:
            data = None
        if data is None or data['parser_version'] != PARSER_VERSION:
            data = extract_archive(path, {CUSIP})
            cache.write_text(json.dumps(data))
            checksum.write_text(digest(cache) + '\n')
        extracted.append(data)
        sources.append({**meta, 'information_rows': data['information_rows'],
                        'cache_sha256': digest(cache), 'target_cusips': [CUSIP]})
    return merge_archives(extracted), sources


def original_documents(root, item):
    """答え合わせの締切を明示。対象選びの既定の締切は緩めない。"""
    body = (root / item['path']).read_bytes()
    if digest(root / item['path']) != item['sha256']:
        raise ValueError('comparison_original_changed')
    _, document, accepted = source_documents(body, item['source'], as_of=OUTCOME_AS_OF)
    return body, document, accepted


def verify_manager_tables(root, catalog):
    """原表の全行を出現回数ごと比較し、表紙の金額合計とも照合する。"""
    original, bulk = {}, {}
    for item in catalog['manager_sources']:
        accession = Path(item['path']).stem
        body, doc, _ = original_documents(root, item)
        primary = xml_primary(doc)
        credentials = [e for e in primary.iter() if split_tag(e.tag)[1] == 'credentials']
        if ([c.zfill(10) for e in credentials for c in fields(e, 'cik')] != [item['source']['cik']]
                or fields(primary, 'periodOfReport') != ['12-31-2025']):
            raise ValueError('comparison_manager_identity_or_period_changed')
        tables = []
        for part in re.findall(rb'<DOCUMENT>(.*?)</DOCUMENT>', body, re.S):
            kind = re.search(rb'<TYPE>([^\r\n]+)', part)
            if kind and kind[1].strip() == b'INFORMATION TABLE':
                tables.extend(re.findall(rb'<XML>\s*(.*?)\s*</XML>', part, re.S))
        if len(tables) != 1:
            raise ValueError('comparison_information_table_missing_or_ambiguous')
        original[accession] = (xml_rows(tables[0]), primary)
        bulk[accession] = []
    # 二つの確認対象はいずれも2026年2月提出。この一括資料を一度だけ走査。
    with ZipFile(root / 'data/sec-bulk/01dec2025-28feb2026_form13f.zip') as archive:
        for row in _table(archive, 'INFOTABLE', ('ACCESSION_NUMBER',)):
            if row['ACCESSION_NUMBER'] in bulk:
                bulk[row['ACCESSION_NUMBER']].append(tsv_row(row))
    verified = []
    for case in catalog['filings']:
        accession = case['accession']; rows, primary = original[accession]
        comparison = compare_tables(rows, bulk[accession])
        if (comparison != case['comparison']
                or fields(primary, 'tableEntryTotal') != [str(case['original_declared_entries'])]
                or fields(primary, 'tableValueTotal') != [str(case['declared_value'])]):
            raise ValueError('comparison_source_table_review_changed')
        verified.append({'accession': accession, **comparison})
    return verified


def verify_share_bridge(bridge, texts):
    """増加分が新規発行と株式報酬でつながるか、確認した原文と計算を照合。"""
    if (bridge['row_sha256'] != [object_hash(t) for t in bridge['rows']]
            or not all(t in texts for t in bridge['rows'])):
        raise ValueError('share_bridge_original_rows_changed')
    if (bridge['beginning_shares'] + bridge['equity_line_issued_shares']
            + bridge['vested_award_shares'] != bridge['ending_shares']
            or bridge['reviewed_unit_multiplier'] != 1):
        raise ValueError('share_bridge_does_not_reconcile')
    return True


def verify_denominators(root, catalog):
    result = {}
    for review in catalog['denominator_reviews']:
        _, doc, accepted = original_documents(root, review['source'])
        cover = parse_cover(doc, CIK, accepted)
        if cover.get('ticker_in_filing') != 'CJMB':
            raise ValueError('comparison_issuer_security_changed')
        blocks = re.findall(r'<TEXT>\s*(.*?)\s*</TEXT>', doc, re.S)
        if len(blocks) != 1:
            raise ValueError('comparison_primary_text_ambiguous')
        data = outstanding_candidates(blocks[0], CIK, review['period'])
        facts = [f for f in data['facts'] if f['fact_id'] == review['fact_id']]
        if (data['shares'] != review['shares'] or len(facts) != 1
                or object_hash(facts[0]) != review['fact_sha256']):
            raise ValueError('comparison_denominator_fact_changed')
        root_element, _ = parse_document(blocks[0])
        parents = {e: p for p in root_element.iter() for e in p}
        nodes = [e for e in root_element.iter() if e.get('id') == review['fact_id']]
        if len(nodes) != 1:
            raise ValueError('comparison_fact_id_ambiguous')
        node = nodes[0]
        while split_tag(node.tag)[1] != 'tr':
            if node not in parents:
                raise ValueError('comparison_balance_sheet_row_missing')
            node = parents[node]
        if object_hash(' '.join(' '.join(node.itertext()).split())) != review['row_sha256']:
            raise ValueError('comparison_balance_sheet_row_changed')
        if review['period'] == END_PERIOD:
            bridge = catalog['share_unit_bridge']
            if review['source']['sha256'] != bridge['source_sha256']:
                raise ValueError('share_bridge_source_changed')
            texts = [' '.join(' '.join(e.itertext()).split()) for e in root_element.iter()
                     if split_tag(e.tag)[1] == 'tr']
            verify_share_bridge(bridge, texts)
        result[review['period']] = {'shares': data['shares'], 'period': review['period'],
                                    'security_class_verified': True, 'source': review['source'],
                                    'available_at_selection_date': False,
                                    'used_for_outcome_comparison_only': True}
    bridge = catalog['share_unit_bridge']
    if (set(result) != {START_PERIOD, END_PERIOD}
            or result[START_PERIOD]['shares'] != bridge['beginning_shares']
            or result[END_PERIOD]['shares'] != bridge['ending_shares']):
        raise ValueError('share_bridge_denominators_changed')
    return result


def reported_change(previous, current):
    """合算の差を、両期に登場する運用者と片期だけの運用者へ分解する。

片期だけの行には実際の保有ゼロを入れず、Noneを残す。引き算の寄与は
報告された合計の内訳であり、実際の売買・新規参入・撤退の認定ではない。
    """
    if previous['cusip'] != current['cusip']:
        raise ValueError('different_security_cannot_be_compared')
    maps = [{m['cik']: m for m in r['ledger']} for r in (previous, current)]
    for mapping, result in zip(maps, (previous, current)):
        if (len(mapping) != len(result['ledger']) or sum(m['shares'] for m in mapping.values())
                != result['reported_share_sum_before_overlap_resolution']):
            raise ValueError('inconsistent_manager_ledger')
    a, b = maps; records = []
    for cik in sorted(a.keys() | b.keys()):
        before = a[cik]['shares'] if cik in a else None
        after = b[cik]['shares'] if cik in b else None
        records.append({'cik': cik, 'name': (b.get(cik) or a[cik])['name'],
                        'previous_reported_shares': before, 'current_reported_shares': after,
                        'presence': 'both' if cik in a and cik in b else 'previous_only' if cik in a else 'current_only',
                        'contribution_to_reported_sum_difference': (after or 0) - (before or 0),
                        'actual_trade_change_confirmed': False})
    groups = {g: sum(r['contribution_to_reported_sum_difference'] for r in records if r['presence'] == g)
              for g in ('both', 'previous_only', 'current_only')}
    delta = current['reported_share_sum_before_overlap_resolution'] - previous['reported_share_sum_before_overlap_resolution']
    if sum(groups.values()) != delta:
        raise ValueError('reported_change_does_not_reconcile')
    return {'diagnostic_reported_share_change': delta, 'decomposition': groups,
            'manager_records': records, 'absence_is_zero_actual_ownership': False,
            'institutional_flow_label': None, 'eligible_for_accuracy_measurement': False}


def compare_ratios(previous, current, denominators, *, unit_reviewed):
    if not unit_reviewed:
        return {'status': 'share_unit_review_required', 'ratio_change_percentage_points': None}
    d0, d1 = (denominators[p]['shares'] for p in (START_PERIOD, END_PERIOD))
    if any(not isinstance(d, int) or isinstance(d, bool) or d <= 0 for d in (d0, d1)):
        raise ValueError('invalid_comparison_denominator')
    p0 = 100 * previous['reported_share_sum_before_overlap_resolution'] / d0
    p1 = 100 * current['reported_share_sum_before_overlap_resolution'] / d1
    return {'status': 'public_reported_holdings_proxy_only', 'previous_ratio_percent': round(p0, 6),
            'current_ratio_percent': round(p1, 6), 'ratio_change_percentage_points': round(p1 - p0, 6),
            'shares_outstanding_change': d1 - d0,
            'shares_outstanding_change_percent': round(100 * (d1 / d0 - 1), 6),
            'actual_institutional_ownership_percent': None}


def vintage_changes(early, late):
    """訂正の提出番号だけ変わった場合と、保有行の内容が変わった場合を区別。"""
    a, b = ({m['cik']: m for m in r['ledger']} for r in (early, late))
    changes = []
    for cik in sorted(a.keys() | b.keys()):
        first, last = a.get(cik), b.get(cik)
        if first == last:
            continue
        before = Counter(object_hash(canonical_row(r)) for r in first['rows']) if first else Counter()
        after = Counter(object_hash(canonical_row(r)) for r in last['rows']) if last else Counter()
        changes.append({'cik': cik, 'previous_accessions': first['accessions'] if first else [],
                        'latest_accessions': last['accessions'] if last else [],
                        'previous_reported_shares': first['shares'] if first else None,
                        'latest_reported_shares': last['shares'] if last else None,
                        'same_reported_position_payloads': before == after})
    return changes


def run(root, fetch=False):
    catalog = json.loads((root / CATALOG_PATH).read_text())
    if (catalog['selection_as_of'] != SELECTION_AS_OF or catalog['outcome_as_of'] != OUTCOME_AS_OF
            or catalog['periods'] != [START_PERIOD, END_PERIOD]
            or catalog['target'] != {'ticker': 'CJMB', 'cusip': CUSIP, 'cik': CIK}
            or digest(root / ADMISSION_PATH) != catalog['admission_evidence_sha256']):
        raise ValueError('comparison_frozen_protocol_changed')
    admission = json.loads((root / ADMISSION_PATH).read_text())
    chosen = [r for r in admission['rows'] if r['ready_for_reported_holdings_pilot_with_listing_proxy']]
    if [r['cusip'] for r in chosen] != [CUSIP]:
        raise ValueError('comparison_selection_changed')
    client = SafeHttp(timeout=25, interval=1.2, max_requests=30, max_seconds=900) if fetch else None
    for item in catalog['issuer_sources'] + catalog['manager_sources']:
        path = root / item['path']
        if not path.exists() and client is None:
            raise ValueError('missing_comparison_source_use_fetch')
        _get_file(client, path, item['source_url'], item['sha256'], max_bytes=24_000_000)
    filings, sources = load_archives(root, client)
    verified = verify_manager_tables(root, catalog)
    reviewed, applications = apply_filing_reviews(filings, catalog)
    if any(r['status'] != 'matched' for r in applications):
        raise ValueError('comparison_review_not_applied')
    denominators = verify_denominators(root, catalog)
    quarters, raw_quarters = {}, {}
    for period in (START_PERIOD, END_PERIOD):
        quarter, checksum = LISTS[period]; path = root / f'data/sec-bulk/13flist{quarter}.txt'
        if not path.exists() and client is None:
            raise ValueError('missing_comparison_security_list_use_fetch')
        meta = _get_file(client, path, f'https://www.sec.gov/files/investment/13flist{quarter}.txt', checksum)
        official = match_security_list(path, CUSIP)
        raw_quarters[period] = aggregate_security(select_filings(filings, period, OUTCOME_AS_OF), CUSIP)
        states = select_filings(reviewed, period, OUTCOME_AS_OF)
        result = aggregate_security(states, CUSIP)
        result['quality'] = quality_summary(result)
        result['same_size_positions'] = same_size_positions(result)
        result['denominator'] = denominators[period]
        result['official_security'] = {'entry': official, 'source': meta}
        accessions = {a for m in result['ledger'] for a in m['accessions']}
        result['included_filing_dates'] = sorted({reviewed[a]['filed'] for a in accessions})
        result['manager_operations'] = {c: s['operations'] for c, s in states.items()
                                        if CUSIP in s['target_cusips_in_history']}
        quarters[period] = result
    early = aggregate_security(select_filings(reviewed, START_PERIOD, '2026-03-01'), CUSIP)
    a, b = quarters[START_PERIOD], quarters[END_PERIOD]
    report = {'version': VERSION, 'target': catalog['target'], 'selection_as_of': SELECTION_AS_OF,
              'outcome_as_of': OUTCOME_AS_OF, 'periods': [START_PERIOD, END_PERIOD],
              'catalog_sha256': digest(root / CATALOG_PATH), 'admission_sha256': digest(root / ADMISSION_PATH),
              'archive_sources': sources, 'source_table_verification': verified,
              'source_review_applications': applications, 'quarters': quarters,
              'before_source_review': {p: {'reported_shares': r['reported_share_sum_before_overlap_resolution'],
                                          'quality': quality_summary(r)} for p, r in raw_quarters.items()},
              'before_source_review_change': reported_change(raw_quarters[START_PERIOD], raw_quarters[END_PERIOD])['diagnostic_reported_share_change'],
              'change': reported_change(a, b), 'ratios': compare_ratios(a, b, denominators, unit_reviewed=True),
              'share_unit_bridge': catalog['share_unit_bridge'], 'drw_reference_review': catalog['drw_reference_review'],
              'previous_period_vintage_check': {'early_as_of': '2026-03-01', 'late_as_of': OUTCOME_AS_OF,
                  'early_reported_shares': early['reported_share_sum_before_overlap_resolution'],
                  'late_reported_shares': a['reported_share_sum_before_overlap_resolution'],
                  'same_active_ledger': early['ledger'] == a['ledger'],
                  'changes': vintage_changes(early, a)},
              'limitations': ['public_13f_not_complete_institutional_ownership', 'unresolved_reference_and_confidential_status',
                              'quarterly_holdings_do_not_identify_trade_days', 'reporting_entity_changes_are_not_proven_trades',
                              'single_security_not_accuracy_sample', 'no_comparison_group_or_low_ownership_threshold',
                              'no_cjmb_volume_signal_scored'],
              'outcomes_used_for_selection_or_signal': False, 'institutional_flow_label': None,
              'accuracy': None, 'returns': None}
    folder = root / 'diagnostics/cjmb'; folder.mkdir(parents=True, exist_ok=True)
    (folder / 'comparison.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='CJMBの二期の公開保有報告を照合・比較する')
    parser.add_argument('--fetch', action='store_true', help='不足する公式資料だけを取得する')
    args = parser.parse_args()
    report = run(Path(__file__).resolve().parents[1], args.fetch)
    print(json.dumps({'reported_share_change': report['change']['diagnostic_reported_share_change'],
                      'before_source_review_change': report['before_source_review_change'],
                      'ratios': report['ratios'], 'accuracy': report['accuracy']}, indent=2))
