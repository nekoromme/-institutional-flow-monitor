"""固定候補の普通株数を追加確認し、比較群の準備状況を再現する。

株数の大小や後の出来高で確認する順番を変えない。可視表と機械用の
数値が離れている書類でも、同社・同日・同じ原書類の両方を照合する。
これは分母の確認だけで、機関保有の集計問題まで解決したとは扱わない。
"""
import json
from pathlib import Path

from .admission_review import MATERIALS, same_size_positions
from .bulk13f import digest
from .cohort import AS_OF, PERIOD
from .eligibility import IDENTITY_PATH, IDENTITY_SHA, primary_text
from .filing_review import object_hash
from .share_facts import outstanding_candidates, parse_document, split_tag

VERSION = 'class-expansion-0.1'
CATALOG = 'docs/evidence/class-expansion-catalog-2026-09-10.json'
PREVIOUS = 'docs/evidence/admission-review-2026-09-10.json'
PREVIOUS_ORDINALS = {4, 179, 189}


def review_queue(rows):
    """既存の順番で次の10件。割合・警告の少なさ・値動きを条件にしない。"""
    return sorted((r for r in rows if r['denominator'].get('shares')
                   and r['ordinal'] not in PREVIOUS_ORDINALS),
                  key=lambda r: r['ordinal'])[:10]


def normalized_text(element):
    return ' '.join(' '.join(element.itertext()).split())


def verify_review(row, identity, document, review):
    """確認記録は、元入力・数値の意味・可視表が全部一致した時だけ使う。

原表を見て判断した結論を別の提出へ使い回さない。機械用の数値だけ
一致しても、可視表や表の列見出しが変わっていれば確認待ちへ戻す。
    """
    if (object_hash(row) != review['eligibility_row_sha256']
            or row['ordinal'] != review['ordinal']
            or row['cusip'] != review['cusip']
            or row['issuer_cik'] != review['issuer_cik']
            or row['denominator']['source'] != review['source']
            or row['denominator']['shares'] != review['shares']
            or review['period'] != PERIOD
            or identity['reviews']['ownership_identity']['source_sha256'] != review['ownership_source_sha256']
            or identity['reviews']['ownership_identity']['security_class'] != review['ownership_security_class']):
        raise ValueError('class_expansion_input_changed')
    # 標準の株数項目・日付・会社・単位を既存の厳密な読み取りで再確認。
    numeric = outstanding_candidates(document, row['issuer_cik'])
    if numeric['status'] != 'exact_period_numeric_candidate' or numeric['shares'] != review['shares']:
        raise ValueError('class_expansion_numeric_fact_changed')
    facts = [f for f in numeric['facts'] if f['fact_id'] == review['fact_id']]
    if len(facts) != 1 or facts[0]['rounded_to_shares'] != review['rounding_to_shares']:
        raise ValueError('class_expansion_fact_or_precision_changed')
    root, _ = parse_document(document)
    parents = {child: parent for parent in root.iter() for child in parent}
    rows = [e for e in root.iter() if split_tag(e.tag)[1] == 'tr'
            and object_hash(normalized_text(e)) == review['balance_sheet_row_sha256']]
    if len(rows) != 1:
        raise ValueError('class_expansion_visible_row_changed_or_ambiguous')
    node = rows[0]
    if normalized_text(node) != review['balance_sheet_row_excerpt']:
        raise ValueError('class_expansion_visible_excerpt_changed')
    if review['fact_location'] == 'inside_reviewed_row':
        if not any(e.get('id') == review['fact_id'] for e in node.iter()):
            raise ValueError('class_expansion_fact_moved_outside_row')
    elif review['fact_location'] != 'separate_numeric_fact_and_visible_reviewed_row':
        raise ValueError('class_expansion_unsupported_review_type')
    table = parents.get(node)
    while table is not None and split_tag(table.tag)[1] != 'table':
        table = parents.get(table)
    if table is None or object_hash(normalized_text(table)) != review['balance_sheet_table_sha256']:
        raise ValueError('class_expansion_table_or_dates_changed')
    if review.get('supporting_row_excerpt') and review['supporting_row_excerpt'] not in normalized_text(table):
        raise ValueError('class_expansion_class_heading_changed')
    if review.get('units_excerpt') and review['units_excerpt'] not in normalized_text(root):
        raise ValueError('class_expansion_units_heading_changed')
    return {'security_class_verified': True, 'shares': numeric['shares'], 'period': PERIOD,
            'rounding_to_shares': review['rounding_to_shares'],
            'source': review['source'], 'fact_location': review['fact_location'],
            'complete_institutional_ownership_confirmed': False}


def inventory(rows, verified):
    """未確認の候補も残す。分母の確認だけで対象への採用に昇格させない。"""
    output = []
    for row in rows:
        status = 'verified' if row['ordinal'] in verified else (
            'numeric_candidate_unreviewed' if row['denominator'].get('shares') else 'denominator_unavailable')
        reasons = list(row['review_reasons'])
        if status == 'verified':
            reasons = [r for r in reasons if r != 'denominator_security_class_not_certified']
        output.append({'ordinal': row['ordinal'], 'cusip': row['cusip'],
                       'ticker': row['ticker_in_historical_filing'], 'class_status': status,
                       'remaining_review_reasons': reasons,
                       'low_ownership_eligible': None, 'eligible_for_accuracy_measurement': False})
    return output


def run(root):
    catalog = json.loads((root / CATALOG).read_text())
    for path, expected in [(MATERIALS, catalog['eligibility_materials_sha256']),
                           (IDENTITY_PATH, IDENTITY_SHA), (PREVIOUS, catalog['previous_review_sha256'])]:
        if digest(root / path) != expected:
            raise ValueError('class_expansion_pinned_input_changed')
    materials = json.loads((root / MATERIALS).read_text())['rows']
    queued = review_queue(materials)
    if [r['ordinal'] for r in queued] != catalog['review_ordinals']:
        raise ValueError('class_expansion_fixed_queue_changed')
    reviews = {r['ordinal']: r for r in catalog['class_reviews']}
    if set(reviews) != set(catalog['review_ordinals']) or len(catalog['class_reviews']) != len(reviews):
        raise ValueError('class_expansion_review_set_changed')
    load = lambda p: json.loads((root / p).read_text())
    identities = {r['ordinal']: r for r in load(IDENTITY_PATH)['rows']}
    proposals = {r['ordinal']: r for r in load('diagnostics/identity/name-candidates.json')['rows']}
    downloads = {r['source']['filename']: r for r in load('diagnostics/identity/source-download.json')['files']}
    fallbacks = {r['ordinal']: r for r in load('diagnostics/identity/primary-cover-download.json')['files']}
    holdings = load('diagnostics/eligibility/holdings-audit.json')
    output = []
    for row in queued:
        n = row['ordinal']
        text, _ = primary_text(root, identities[n], proposals[n], downloads, fallbacks)
        verified = verify_review(row, identities[n], text, reviews[n])
        holding = holdings[row['cusip']]
        if object_hash(holding) != catalog['holdings_audit_sha256_by_cusip'][row['cusip']]:
            raise ValueError('class_expansion_holdings_input_changed')
        output.append({'ordinal': n, 'ticker': row['ticker_in_historical_filing'],
                       'cusip': row['cusip'], 'denominator_review': verified,
                       'reported_shares_before_overlap_resolution': row['shares_reported_before_overlap_resolution'],
                       'diagnostic_ratio_percent': row['diagnostic_reported_sum_divided_by_candidate_shares_percent'],
                       'holdings_quality': row['holdings_quality'],
                       'same_size_cross_manager_position_pairs': len(same_size_positions(holding)),
                       'same_size_is_overlap_proof': False, 'automatically_subtracted': 0,
                       'listing_at_selection_date_confirmed': False,
                       'low_ownership_eligible': None, 'eligible_for_accuracy_measurement': False})
    prior = load(PREVIOUS)['rows']
    if {r['ordinal'] for r in prior if r['denominator_review']['security_class_verified']} != PREVIOUS_ORDINALS:
        raise ValueError('class_expansion_previous_review_changed')
    all_verified = PREVIOUS_ORDINALS | set(reviews)
    report = {'version': VERSION, 'selection_as_of': AS_OF, 'holdings_period': PERIOD,
              'catalog_sha256': digest(root / CATALOG), 'selection_rule': catalog['selection_rule'],
              'rows': output, 'candidate_inventory': inventory(materials, all_verified),
              'summary': {'fixed_candidates': len(materials), 'additional_class_reviews': len(output),
                          'total_class_reviews': len(all_verified),
                          'numeric_candidates_still_unreviewed': sum(bool(r['denominator'].get('shares')) and r['ordinal'] not in all_verified for r in materials),
                          'separate_numeric_fact_and_visible_row': sum(r['denominator_review']['fact_location'] == 'separate_numeric_fact_and_visible_reviewed_row' for r in output),
                          'rounded_denominators': sum(r['denominator_review']['rounding_to_shares'] > 1 for r in output),
                          'new_accuracy_eligible': 0, 'network_requests': 0},
              'limitations': ['class_review_only_not_holdings_aggregation_clearance',
                              'pre_cutoff_filing_listing_not_selection_date_listing_confirmation',
                              'comparison_group_and_low_ownership_rule_not_finalized',
                              'no_signal_return_or_accuracy_result']}
    folder = root / 'diagnostics/class-expansion'; folder.mkdir(parents=True, exist_ok=True)
    (folder / 'review.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return report


if __name__ == '__main__':
    print(json.dumps(run(Path(__file__).resolve().parents[1])['summary'], indent=2))
