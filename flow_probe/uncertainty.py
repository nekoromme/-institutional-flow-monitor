"""未解決の参照を優先順位付けする。関連株数を誤差幅とは扱わない。"""
import json
from pathlib import Path

from .bulk13f import digest


def triage(audit, ledgers):
    output = {}
    for period, quarter in audit['quarters'].items():
        output[period] = {}
        for symbol, item in quarter['symbols'].items():
            managers = {r['cik']: r for r in ledgers[f'{period}/{symbol}']}
            groups = {}
            for ref in item['unresolved_manager_references']:
                kind = ('cover_reference_target_link_unproven' if ref.get('relationship') == 'reported_by'
                        else 'holding_row_reference')
                group = groups.setdefault((ref['cik'], kind), {'cik': ref['cik'], 'kind': kind,
                    'name': managers[ref['cik']]['name'], 'reference_count': 0,
                    'manager_reported_shares': managers[ref['cik']]['shares'], 'accessions': set()})
                group['reference_count'] += 1
                group['accessions'].add(ref['accession'])
            queue = [{**g, 'accessions': sorted(g['accessions'])} for g in groups.values()]
            touched = {g['cik'] for g in queue}
            for edge in item['potential_overlap_relationships']:
                touched.update(edge['ciks'])
            for row in item['repeated_row_payloads']:
                touched.add(row['cik'])
            associated = sum(managers[c]['shares'] for c in touched)
            output[period][symbol] = {
                'unresolved_reference_count': len(item['unresolved_manager_references']),
                'reference_review_queue': sorted(queue, key=lambda g: (-g['manager_reported_shares'], g['cik'], g['kind'])),
                'potential_overlap_relationships': item['potential_overlap_relationships'],
                'managers_touched_by_references_overlap_or_repeated_rows': len(touched),
                'associated_manager_shares_counted_once': associated,
                'associated_shares_are_error_bound': False,
                'unresolved_filing_count': len(item['unresolved_filings']),
                'unreviewed_class_row_count': len(item['unreviewed_class_rows']),
                'certified_holdings_change': None, 'low_ownership_eligible': None,
            }
    return {'version': 'holdings-uncertainty-0.1', 'as_of': audit['as_of'],
            'policy': 'retain_diagnostic_totals; no_automatic_subtraction; unknown_labels_excluded_from_accuracy',
            'interpretation': 'associated_manager_shares_are_work_priority_not_duplicate_or_missing_share_estimate',
            'quarters': output}


def public_summary(report):
    report = json.loads(json.dumps(report))
    for quarter in report['quarters'].values():
        for symbol, item in quarter.items():
            queue = item.pop('reference_review_queue')
            item['reference_review_queue_count'] = len(queue)
            item['reference_review_queue_sample'] = queue[:8 if symbol in {'UAVS', 'QMCO'} else 3]
            edges = item.pop('potential_overlap_relationships')
            item['potential_overlap_relationships_count'] = len(edges)
            item['potential_overlap_relationships_sample'] = edges[:3]
    report['detail_policy'] = 'bounded_examples; full_local_queue_reproducible_with_flow_probe.uncertainty'
    return report


def main():
    root = Path(__file__).resolve().parents[1]
    audit = root / 'diagnostics/holdings/bulk-audit.json'
    ledger = root / 'diagnostics/holdings/manager-ledger.json'
    report = triage(json.loads(audit.read_text()), json.loads(ledger.read_text()))
    report['source_sha256'] = {'audit': digest(audit), 'ledger': digest(ledger)}
    path = root / 'diagnostics/holdings/uncertainty.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    path.with_name('uncertainty-summary.json').write_text(
        json.dumps(public_summary(report), ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
