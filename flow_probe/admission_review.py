"""対象選定の保留を、原書類の根拠と対応づけて少しずつ解消する。

ここで作るのは過去の公開保有報告を比較するための材料。
全機関の保有率、低保有の境界値、売買の推奨を認定する処理ではない。
前回の結果を上書きせず、確認済みの部分だけを別の記録として残す。
"""
import argparse
from collections import defaultdict
from itertools import combinations
import json
from pathlib import Path
from xml.etree import ElementTree as ET

from .bulk13f import digest
from .cohort import AS_OF, PERIOD
from .eligibility import IDENTITY_PATH, IDENTITY_SHA, primary_text
from .filing_review import object_hash
from .holdings import _get_file
from .http_client import SafeHttp
from .identity import read_indexes
from .identity_sources import parse_cover, source_documents
from .share_facts import parse_document, split_tag

VERSION = 'admission-review-0.1'
CATALOG = 'docs/evidence/admission-review-catalog-2026-09-10.json'
MATERIALS = 'docs/evidence/eligibility-materials-2026-09-10.json'
EVENT_FORMS = {'8-K', '8-K/A', '10-Q', '10-K', '25', '25-NSE', '15-12B', '15-12G'}


def review_queue(rows):
    """値動き・保有割合を使わず、未解決の集計項目が少ない群を選ぶ。

機密欄が不明なだけのものは原表を確認する候補に含めるが、解決済みに
変更するわけではない。新たな問題が見つかっても最初の選択を記録に残す。
    """
    return [r for r in rows if r['denominator'].get('shares')
            and r['identity_verified'] and not r['historical_index_flags']
            and all(v == 0 for k, v in r['holdings_quality']['counts'].items()
                    if k != 'confidential_status_unknown_managers')]


def same_size_positions(holding):
    """別の運用者の同じ株数を手掛かりとして保存。重複とは断定しない。

関連会社の参照番号がなくても、同じ顧客の保有を含む可能性は残る。
反対に、同じ株数を別々に持つこともあるので、自動では差し引かない。
    """
    sizes = defaultdict(set)
    for manager in holding['ledger']:
        for row in manager['rows']:
            if row['shares'] > 0:
                sizes[row['shares']].add(manager['cik'])
    return [{'shares_each': shares, 'manager_ciks': [a, b],
             'overlap_confirmed': False, 'automatically_subtracted': 0}
            for shares, managers in sorted(sizes.items())
            for a, b in combinations(sorted(managers), 2)]


def verify_class_review(row, identity, document, review):
    """手作業の照合結果は、会社・日付・元書類・数値が同じ時だけ使う。"""
    if (object_hash(row) != review['eligibility_row_sha256']
            or row['cusip'] != review['cusip'] or row['issuer_cik'] != review['issuer_cik']
            or row['denominator']['source']['source_sha256'] != review['denominator_source_sha256']
            or row['denominator']['shares'] != review['shares']
            or row['denominator']['period'] != review['period'] or review['period'] != PERIOD
            or identity['reviews']['ownership_identity']['source_sha256'] != review['ownership_source_sha256']
            or identity['reviews']['ownership_identity']['security_class'] != review['ownership_security_class']):
        raise ValueError('class_review_input_changed')
    root, _ = parse_document(document)
    facts = [e for e in root.iter() if e.get('id') == review['fact_id']]
    if len(facts) != 1:
        raise ValueError('reviewed_fact_missing_or_duplicated')
    parents = {child: parent for parent in root.iter() for child in parent}
    node = facts[0]
    while split_tag(node.tag)[1] != 'tr':
        if node not in parents:
            raise ValueError('reviewed_balance_sheet_row_missing')
        node = parents[node]
    text = ' '.join(' '.join(node.itertext()).split())
    if object_hash(text) != review['balance_sheet_row_sha256']:
        raise ValueError('reviewed_balance_sheet_row_changed')
    return {'security_class_verified': True, 'period': PERIOD, 'shares': review['shares'],
            'source_sha256': review['denominator_source_sha256'],
            'fact_id': review['fact_id'], 'review_conclusion': review['review_conclusion'],
            'current_shares_outstanding': None}


def primary_confidential_status(body, expected_cik):
    """原表にも欄がなければ不明のまま。省略をfalseと推測しない。"""
    root = ET.fromstring(body)
    def values(name, parent=root):
        return [''.join(e.itertext()).strip() for e in parent.iter()
                if split_tag(e.tag)[1] == name]
    credentials = [e for e in root.iter() if split_tag(e.tag)[1] == 'credentials']
    ciks = [v.zfill(10) for p in credentials for v in values('cik', p)]
    if ciks != [expected_cik] or values('periodOfReport') != ['09-30-2025']:
        raise ValueError('manager_primary_identity_or_period_mismatch')
    entries = values('isConfidentialOmitted')
    if not entries:
        return {'status': 'unknown', 'reason': 'field_absent_in_original_xml'}
    if len(entries) != 1 or entries[0] not in {'true', 'false', '1', '0'}:
        raise ValueError('ambiguous_original_confidential_indicator')
    return {'status': 'declared' if entries[0] in {'true', '1'} else 'not_declared',
            'reason': 'explicit_original_xml_field'}


def pilot_material_status(row, class_review, listing_evidence, equal_positions):
    # 上場は最終取引日の取引所名簿で確定したものではなく、当時の届出に
    # 基づく代理情報。研究でこの仮定を許す場合に使える材料として区別する。
    ready = (class_review['security_class_verified'] and bool(listing_evidence)
             and row['holdings_quality']['no_detected_aggregation_issues']
             and not equal_positions)
    return {'ready_for_reported_holdings_pilot_with_listing_proxy': ready,
            'listing_at_selection_date_confirmed': False,
            'complete_institutional_ownership_confirmed': False,
            'low_ownership_eligible': None, 'eligible_for_accuracy_measurement': False}


def run(root, fetch=False):
    catalog = json.loads((root / CATALOG).read_text())
    if (digest(root / MATERIALS) != catalog['eligibility_materials_sha256']
            or digest(root / IDENTITY_PATH) != IDENTITY_SHA):
        raise ValueError('admission_pinned_input_changed')
    materials = json.loads((root / MATERIALS).read_text())
    numeric = [r for r in materials['rows'] if r['denominator'].get('shares')]
    queued = review_queue(materials['rows'])
    if [r['ordinal'] for r in queued] != [4, 179, 189]:
        raise ValueError('fixed_review_queue_changed')
    # 四半期索引から作り直し、都合の悪い届出が確認計画から消えないようにする。
    _, index, _ = read_indexes(root)
    planned = [s for r in queued for s in index[r['issuer_cik']]
               if s['form'] in EVENT_FORMS and s['filed'] >= PERIOD]
    sources = catalog['issuer_sources']['files']
    if sorted(object_hash(s) for s in planned) != sorted(object_hash(s['source']) for s in sources):
        raise ValueError('historical_event_source_plan_changed')
    client = SafeHttp(interval=1.2, timeout=25, max_requests=65, max_seconds=900) if fetch else None
    for item in sources + catalog['manager_sources']['files']:
        path = root / item['path']
        if not path.exists() and client is None:
            raise ValueError('missing_review_source_use_fetch')
        # 既存ファイルの変更は上書きせず停止。再取得も公開GETのみ。
        _get_file(client, path, item['url'], item['sha256'], max_bytes=24_000_000)
    holdings = json.loads((root / 'diagnostics/eligibility/holdings-audit.json').read_text())
    identities = {r['ordinal']: r for r in json.loads((root / IDENTITY_PATH).read_text())['rows']}
    proposals = {r['ordinal']: r for r in json.loads((root / 'diagnostics/identity/name-candidates.json').read_text())['rows']}
    downloads = {r['source']['filename']: r for r in json.loads((root / 'diagnostics/identity/source-download.json').read_text())['files']}
    fallbacks = {r['ordinal']: r for r in json.loads((root / 'diagnostics/identity/primary-cover-download.json').read_text())['files']}
    rows, cover_records = [], []
    for item in sources:
        _, document, accepted = source_documents((root / item['path']).read_bytes(), item['source'])
        cover = parse_cover(document, item['source']['cik'], accepted)
        cover_records.append({'source': item['source'], 'source_sha256': item['sha256'],
                              'accepted_at_new_york': accepted, 'cover': cover})
    for row, review in zip(queued, catalog['class_reviews']):
        n = row['ordinal']
        text, _ = primary_text(root, identities[n], proposals[n], downloads, fallbacks)
        verified = verify_class_review(row, identities[n], text, review)
        holding = holdings[row['cusip']]
        if (holding['reported_share_sum_before_overlap_resolution'] != row['shares_reported_before_overlap_resolution']
                or object_hash(holding) != catalog['holdings_audit_sha256_by_cusip'][row['cusip']]):
            raise ValueError('holdings_review_input_changed')
        covers = [c for c in cover_records if c['source']['cik'] == row['issuer_cik']]
        covers.sort(key=lambda c: (c['source']['filed'], c['source']['filename']))
        latest = covers[-1]
        same_ticker = latest['cover'].get('ticker_in_filing') == row['ticker_in_historical_filing']
        equals = same_size_positions(holding)
        rows.append({'ordinal': n, 'cusip': row['cusip'], 'ticker': row['ticker_in_historical_filing'],
                     'denominator_review': verified, 'listing_evidence': latest,
                     'listing_proxy_assumption': 'latest_pre_cutoff_filing_registration; not_exchange_session_confirmation',
                     'reported_shares': row['shares_reported_before_overlap_resolution'],
                     'diagnostic_period_ratio_percent': row['diagnostic_reported_sum_divided_by_candidate_shares_percent'],
                     'holdings_quality': row['holdings_quality'], 'same_size_cross_manager_positions': equals,
                     **pilot_material_status(row, verified, same_ticker, equals)})
    manager_reviews = [{**item, 'review': primary_confidential_status((root / item['path']).read_bytes(), item['cik'])}
                       for item in catalog['manager_sources']['files']]
    report = {'version': VERSION, 'selection_as_of': AS_OF, 'holdings_period': PERIOD,
              'catalog_sha256': digest(root / CATALOG), 'selection_uses_returns_or_ratio': False,
              'review_queue': [r['ordinal'] for r in queued], 'rows': rows,
              'numeric_candidate_inventory': [
                  {'ordinal': r['ordinal'], 'cusip': r['cusip'],
                   'ticker': r['ticker_in_historical_filing'],
                   'historical_index_flags': r['historical_index_flags'],
                   'holdings_quality_counts': r['holdings_quality']['counts'],
                   'selected_for_first_class_review': r['ordinal'] in {q['ordinal'] for q in queued},
                   'class_review_remaining': r['ordinal'] not in {q['ordinal'] for q in queued}}
                  for r in numeric],
              'issuer_cover_records': cover_records, 'manager_primary_reviews': manager_reviews,
              'event_reviews': catalog['event_reviews'],
              'summary': {'numeric_candidates': len(numeric), 'class_reviews_completed': len(rows),
                          'original_issuer_filings_checked': len(sources),
                          'original_manager_covers_checked': len(manager_reviews),
                          'pilot_materials_ready_with_listing_proxy': sum(r['ready_for_reported_holdings_pilot_with_listing_proxy'] for r in rows),
                          'accuracy_measurement_eligible': 0},
              'limitations': ['review_subset_not_representative', 'listing_proxy_not_continuous_listing_proof',
                              'public_13f_holdings_not_complete_institutional_ownership',
                              'no_low_ownership_threshold_or_comparison_group_fixed',
                              'no_predictive_performance_result']}
    out = root / 'diagnostics/admission'; out.mkdir(parents=True, exist_ok=True)
    (out / 'review.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='普通株・過去の上場記載・保有集計の確認記録を再現する')
    parser.add_argument('--fetch', action='store_true', help='不足する確認用原書類だけを公開経路で取得する')
    args = parser.parse_args()
    print(json.dumps(run(Path(__file__).resolve().parents[1], args.fetch)['summary'], indent=2))
