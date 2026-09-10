"""残る52件を一度に確認する。個別の保留で他銘柄の作業を止めない。"""
import json
from pathlib import Path
from .admission_review import MATERIALS
from .bulk13f import digest
from .class_expansion import normalized_text, verify_review
from .eligibility import IDENTITY_PATH, IDENTITY_SHA, primary_text
from .filing_review import object_hash
from .share_facts import outstanding_candidates, parse_document

CATALOG = 'docs/evidence/class-full-catalog-2026-09-10.json'
PREVIOUS = 'docs/evidence/class-expansion-2026-09-10.json'
VERSION = 'class-full-0.1'


def verify_fragment(row, identity, document, review):
    """表をまたぐ説明・表紙・注記は、確認した全文片と標準の株数を照合。

別の数値を近くから適当に拾わない。元書類はprimary_textで指紋と
公表日を検査済み。目視照合の結論は、確認した同じ入力に限定する。
    """
    if (object_hash(row) != review['eligibility_row_sha256']
            or row['denominator']['source'] != review['source']
            or identity['reviews']['ownership_identity']['source_sha256'] != review['ownership_source_sha256']
            or identity['reviews']['ownership_identity']['security_class'] != review['ownership_security_class']):
        raise ValueError('fragment_identity_or_input_changed')
    data = outstanding_candidates(document, row['issuer_cik'])
    facts = [f for f in data.get('facts', []) if f['fact_id'] == review['fact_id']]
    if (data['status'] != 'exact_period_numeric_candidate' or data['shares'] != review['shares']
            or len(facts) != 1 or facts[0]['rounded_to_shares'] != review['rounding_to_shares']):
        raise ValueError('fragment_numeric_fact_changed')
    fragment = review['reviewed_fragment']
    if (object_hash(fragment) != review['fragment_sha256']
            or normalized_text(parse_document(document)[0]).count(fragment) != 1):
        raise ValueError('reviewed_fragment_missing_changed_or_ambiguous')
    return {'security_class_verified': True, 'shares': data['shares'], 'period': '2025-09-30',
            'rounding_to_shares': review['rounding_to_shares'], 'source': review['source']}


def run(root):
    load = lambda p: json.loads((root / p).read_text())
    catalog = load(CATALOG)
    for path, sha in [(MATERIALS, catalog['materials_sha256']), (PREVIOUS, catalog['previous_sha256']), (IDENTITY_PATH, IDENTITY_SHA)]:
        if digest(root / path) != sha:
            raise ValueError('full_class_pinned_input_changed')
    materials = load(MATERIALS)['rows']
    previous = {r['ordinal'] for r in load(PREVIOUS)['candidate_inventory'] if r['class_status'] == 'verified'}
    expected = [r['ordinal'] for r in materials if r['denominator'].get('shares') and r['ordinal'] not in previous]
    if expected != catalog['review_ordinals'] or expected != [r['ordinal'] for r in catalog['reviews']]:
        raise ValueError('full_class_review_scope_changed')
    identities = {r['ordinal']: r for r in load(IDENTITY_PATH)['rows']}
    proposals = {r['ordinal']: r for r in load('diagnostics/identity/name-candidates.json')['rows']}
    downloads = {r['source']['filename']: r for r in load('diagnostics/identity/source-download.json')['files']}
    fallbacks = {r['ordinal']: r for r in load('diagnostics/identity/primary-cover-download.json')['files']}
    by_ordinal = {r['ordinal']: r for r in materials}
    results = []
    for review in catalog['reviews']:
        n = review['ordinal']; row = by_ordinal[n]
        result = {'ordinal': n, 'ticker': row['ticker_in_historical_filing'], 'status': 'held'}
        try:
            document, _ = primary_text(root, identities[n], proposals[n], downloads, fallbacks)
            if review['status'] == 'held':
                result['reason'] = review['reason']
            else:
                validator = verify_review if review['verification_mode'] == 'numeric_fact_and_reviewed_table' else verify_fragment
                result.update(status='verified', review=validator(row, identities[n], document, review))
        except (ValueError, KeyError) as exc:
            # 認定を増やすために例外を握りつぶさない。保留理由を出力する。
            result['reason'] = str(exc)[:150]
        results.append(result)
    verified = previous | {r['ordinal'] for r in results if r['status'] == 'verified'}
    report = {'version': VERSION, 'catalog_sha256': digest(root / CATALOG), 'results': results,
              'verified_ordinals': sorted(verified),
              'summary': {'remaining_candidates_checked': len(results), 'new_verified': len(verified - previous),
                          'held': sum(r['status'] == 'held' for r in results), 'total_verified': len(verified),
                          'numeric_candidates_unchecked': 0, 'denominator_unavailable': 135,
                          'certified_complete_institutional_ownership': 0, 'network_requests': 0}}
    folder = root / 'diagnostics/class-full'; folder.mkdir(parents=True, exist_ok=True)
    (folder / 'review.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return report


if __name__ == '__main__':
    print(json.dumps(run(Path(__file__).resolve().parents[1])['summary'], indent=2))
