"""当時の対象選定に必要な材料を、全200件の確認表へ集める。

銘柄の対応、上場状況、保有集計の品質、分母の株式種類は別の確認事項。
数字を割れたことだけで、低機関保有や検証対象への採用を認定しない。
ネットワーク通信は行わず、前段階で取得した公式資料を再利用する。
"""
import json
import re
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET

from .bulk13f import PARSER_VERSION, digest, extract_archive, merge_archives
from .cohort import AS_OF, PERIOD, PRIOR_ARCHIVE, PRIOR_SHA, frozen_states
from .holdings import ARCHIVES, aggregate_security
from .identity_sources import source_documents, source_header
from .share_facts import outstanding_candidates

VERSION = 'eligibility-materials-0.1'
IDENTITY_PATH = 'docs/evidence/identity-review-full-2026-09-10.json'
IDENTITY_SHA = 'af3f90c740159e75e672673ed06871a643f366d816fc29dbe0576fe8b1d39a79'
SOURCES = {PRIOR_ARCHIVE: PRIOR_SHA, '01dec2025-28feb2026_form13f.zip': ARCHIVES['01dec2025-28feb2026_form13f.zip']}


def load_holdings(root, cusips):
    """2025年9月末を報告し、2026年初より前に提出された表を選ぶ。

2026年3月以降の提出分は今回の締切後なので、再走査しない。
訂正で置き換える処理は、既存の保有資料読み取り処理を共用する。
    """
    folder = root / 'data/sec-eligibility'; folder.mkdir(exist_ok=True)
    extracted, manifests = [], []
    for name, checksum in SOURCES.items():
        path = root / 'data/sec-bulk' / name
        if digest(path) != checksum:
            raise ValueError('eligibility_archive_changed')
        cache = folder / (name + '.extracted.json')
        cache_hash = cache.with_suffix(cache.suffix + '.sha256')
        if not cache.exists() or not cache_hash.exists():
            result = extract_archive(path, cusips)
            cache.write_text(json.dumps(result))
            cache_hash.write_text(digest(cache) + '\n')
        if digest(cache) != cache_hash.read_text().strip():
            raise ValueError('eligibility_extraction_cache_changed')
        data = json.loads(cache.read_text())
        if data['sha256'] != checksum or data['target_cusips'] != sorted(cusips):
            raise ValueError('eligibility_extraction_scope_changed')
        if data['parser_version'] != PARSER_VERSION:
            data = extract_archive(path, cusips)
            cache.write_text(json.dumps(data))
            cache_hash.write_text(digest(cache) + '\n')
        extracted.append(data)
        manifests.append({'archive': name, 'sha256': checksum, 'cache_sha256': digest(cache),
                          'information_rows': data['information_rows'],
                          'source_url': 'https://www.sec.gov/files/structureddata/data/form-13f-data-sets/' + name})
    states, audit = frozen_states(merge_archives(extracted))
    return states, manifests, audit


def primary_text(root, row, proposal, downloads, fallbacks):
    """銘柄照合で確認した、その提出書類だけを読む。変更された原文は拒否する。"""
    source = proposal['selected_sources']['listing_cover']
    reviewed = row['reviews']['listing_cover']
    if reviewed.get('acquisition') == 'primary_document_plus_original_header':
        fallback = fallbacks[row['ordinal']]
        if fallback['source'] != source or fallback['cik'] != row['issuer_candidate_cik']:
            raise ValueError('denominator_fallback_identity_changed')
        for part, key in [('primary', 'source_sha256'), ('header', 'header_source_sha256')]:
            if digest(root / fallback[part]['path']) != reviewed[key]:
                raise ValueError('denominator_original_changed')
        source_header((root / fallback['header']['path']).read_bytes(), source)
        text = (root / fallback['primary']['path']).read_text()
    else:
        entry = downloads[source['filename']]
        path = root / entry['path']
        if digest(path) != reviewed['source_sha256']:
            raise ValueError('denominator_original_changed')
        _, doc, _ = source_documents(path.read_bytes(), source)
        blocks = re.findall(r'<TEXT>\s*(.*?)\s*</TEXT>', doc, flags=re.S)
        if len(blocks) != 1:
            raise ValueError('missing_or_ambiguous_document_text')
        text = blocks[0]
    return text, {k: reviewed[k] for k in ('source_url', 'source_sha256', 'form', 'filed')}


def quality_summary(result):
    keys = {'unresolved_filings': 'unresolved_filings',
            'potential_overlap_relationships': 'potential_overlaps',
            'unresolved_manager_references': 'unresolved_references',
            'repeated_row_payloads': 'repeated_rows',
            'unreviewed_class_rows': 'unreviewed_class_rows',
            'confidential_omission_managers': 'confidential_omission_managers',
            'confidential_status_unknown_managers': 'confidential_status_unknown_managers'}
    counts = {label: len(result[key]) for key, label in keys.items()}
    return {'counts': counts, 'no_detected_aggregation_issues': not any(counts.values()),
            'complete_institutional_coverage_confirmed': False}


def selection_row(identity, holding, denominator):
    reasons = []
    if not identity['identity_verified'] or not identity['ticker_in_reviewed_filing']:
        reasons.append('historical_security_identity_incomplete')
    reasons.extend(identity['review_flags'])
    reasons.append('listing_at_selection_date_not_confirmed')
    if denominator.get('status') != 'exact_period_numeric_candidate':
        reasons.append('exact_period_denominator_unavailable_or_ambiguous')
    reasons.append('denominator_security_class_not_certified')
    quality = quality_summary(holding)
    if not quality['no_detected_aggregation_issues']:
        reasons.append('reported_holdings_aggregation_requires_review')
    shares = denominator.get('shares')
    ratio = (round(100 * holding['reported_share_sum_before_overlap_resolution'] / shares, 6)
             if shares and identity['ticker_in_reviewed_filing'] else None)
    if ratio is not None and ratio > 100:
        reasons.append('diagnostic_ratio_exceeds_100_percent_requires_review')
    return {'ordinal': identity['ordinal'], 'cusip': identity['cusip'], 'issuer': identity['issuer'],
            'issuer_cik': identity['issuer_candidate_cik'], 'ticker_in_historical_filing': identity['ticker_in_reviewed_filing'],
            'identity_verified': identity['identity_verified'], 'historical_index_flags': identity['review_flags'],
            'listing_status': 'unconfirmed_at_selection_date',
            'shares_reported_before_overlap_resolution': holding['reported_share_sum_before_overlap_resolution'],
            'positive_reporting_managers': holding['positive_reporting_managers'],
            'holdings_quality': quality, 'denominator': denominator,
            'diagnostic_reported_sum_divided_by_candidate_shares_percent': ratio,
            'ratio_status': 'calculation_for_review_not_certified_ownership' if ratio is not None else 'unavailable',
            'review_reasons': sorted(set(reasons)), 'institutional_ownership_percent': None,
            'low_ownership_eligible': None, 'eligible_for_backtest': False}


def run(root):
    identity_file = root / IDENTITY_PATH
    if digest(identity_file) != IDENTITY_SHA:
        raise ValueError('identity_review_input_changed')
    identities = json.loads(identity_file.read_text())
    proposals = {r['ordinal']: r for r in json.loads((root / 'diagnostics/identity/name-candidates.json').read_text())['rows']}
    downloads = {r['source']['filename']: r for r in json.loads((root / 'diagnostics/identity/source-download.json').read_text())['files']}
    fallbacks = {r['ordinal']: r for r in json.loads((root / 'diagnostics/identity/primary-cover-download.json').read_text())['files']}
    states, manifests, audit = load_holdings(root, {r['cusip'] for r in identities['rows']})
    folder = root / 'diagnostics/eligibility'; folder.mkdir(parents=True, exist_ok=True)
    rows, holding_audits = [], {}
    for identity in identities['rows']:
        holding = aggregate_security(states, identity['cusip'])
        holding_audits[identity['cusip']] = holding
        denominator = {'status': 'identity_not_ready', 'shares': None, 'security_class_verified': False}
        if identity['ticker_in_reviewed_filing']:
            # 指紋の不一致など入力の問題は例外で止め、読み取り未対応とは分ける。
            text, source = primary_text(root, identity, proposals[identity['ordinal']], downloads, fallbacks)
            try:
                denominator = outstanding_candidates(text, identity['issuer_candidate_cik'])
            except (ET.ParseError, ValueError) as exc:
                denominator = {'status': 'document_parse_requires_review', 'shares': None,
                               'reason': str(exc)[:150], 'security_class_verified': False}
            denominator['source'] = source
        rows.append(selection_row(identity, holding, denominator))
        # 途中終了時も、終わった銘柄と残りの範囲が分かるようにする。
        (folder / 'progress.json').write_text(json.dumps({'completed_candidates': len(rows), 'total': 200,
                                                        'last_ordinal': identity['ordinal']}) + '\n')
        if len(rows) % 20 == 0:
            print(json.dumps({'reviewed': len(rows), 'total': 200}), flush=True)
    (folder / 'holdings-audit.json').write_text(json.dumps(holding_audits, ensure_ascii=False) + '\n')
    report = {'version': VERSION, 'selection_as_of': AS_OF, 'holdings_period': PERIOD,
              'identity_review_sha256': IDENTITY_SHA, 'input_frame_sha256': identities['input_frame_sha256'],
              'sources': manifests, 'filing_cutoff_audit': audit,
              'policy': {'uses_only_pre_cutoff_filings': True, 'uses_exact_period_denominators_only': True,
                         'reuses_original_filings': True, 'network_requests': 0,
                         'prior_five_symbol_source_exceptions_reused': False,
                         'low_ownership_threshold_selected': False},
              'summary': {'candidates': len(rows), 'ticker_linked': sum(bool(r['ticker_in_historical_filing']) for r in rows),
                          'exact_period_numeric_denominators': sum(r['denominator']['status'] == 'exact_period_numeric_candidate' for r in rows),
                          'diagnostic_ratios_available': sum(r['diagnostic_reported_sum_divided_by_candidate_shares_percent'] is not None for r in rows),
                          'ratio_over_100_percent': sum('diagnostic_ratio_exceeds_100_percent_requires_review' in r['review_reasons'] for r in rows),
                          'no_detected_holdings_aggregation_issues': sum(r['holdings_quality']['no_detected_aggregation_issues'] for r in rows),
                          'eligible_for_backtest': 0},
              'denominator_status_counts': dict(Counter(r['denominator']['status'] for r in rows)), 'rows': rows}
    (folder / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return report


if __name__ == '__main__':
    report = run(Path(__file__).resolve().parents[1])
    print(json.dumps(report['summary'], indent=2))
