"""固定した54銘柄の二期の保有報告を一括集計する。元資料は再利用。"""
import json
from pathlib import Path
from .batch_common import load_protocol, observed_change, PROTOCOL_SHA
from .bulk13f import PARSER_VERSION, digest, extract_archive, merge_archives, select_filings
from .cjmb_pilot import COMPARISON, COMPARISON_SHA
from .eligibility import quality_summary
from .holdings import ARCHIVES, LISTS, aggregate_security, match_security_list


def run(root):
    protocol = load_protocol(root); targets = {r['cusip'] for r in protocol['symbols']}
    folder = root / 'data/sec-batch'; folder.mkdir(parents=True, exist_ok=True)
    datasets, sources = [], []
    for name, checksum in ARCHIVES.items():
        path = root / 'data/sec-bulk' / name
        if digest(path) != checksum:
            raise ValueError('batch_original_archive_changed')
        cache = folder / (name + '.extracted.json'); sidecar = cache.with_suffix('.json.sha256')
        if cache.exists():
            if not sidecar.exists() or digest(cache) != sidecar.read_text().strip():
                raise ValueError('batch_extraction_cache_changed')
            data = json.loads(cache.read_text())
            if data['sha256'] != checksum or data['target_cusips'] != sorted(targets) or data['parser_version'] != PARSER_VERSION:
                raise ValueError('batch_extraction_scope_or_parser_changed')
        else:
            print(json.dumps({'extracting': name, 'symbols': len(targets)}), flush=True)
            data = extract_archive(path, targets)
            cache.write_text(json.dumps(data)); sidecar.write_text(digest(cache) + '\n')
        datasets.append(data)
        sources.append({'archive': name, 'sha256': checksum, 'cache_sha256': digest(cache),
                        'information_rows': data['information_rows']})
    filings = merge_archives(datasets)
    if digest(root / COMPARISON) != COMPARISON_SHA:
        raise ValueError('batch_reviewed_cjmb_comparison_changed')
    cjmb = json.loads((root / COMPARISON).read_text())
    quarters, ledgers = {}, {}
    for period in protocol['holdings_periods']:
        list_name, checksum = LISTS[period]; path = root / 'data/sec-bulk' / ('13flist' + list_name + '.txt')
        if digest(path) != checksum:
            raise ValueError('batch_official_security_list_changed')
        states = select_filings(filings, period, protocol['outcome_as_of'])
        quarters[period] = {}
        for target in protocol['symbols']:
            symbol, cusip = target['symbol'], target['cusip']
            holding = aggregate_security(states, cusip)
            # CJMBに限り、原表を全行照合済みの別記録を利用。他銘柄へ流用しない。
            if symbol == 'CJMB':
                holding = cjmb['quarters'][period]
            try:
                official = match_security_list(path, cusip); status = 'matched'
            except ValueError:
                official = None; status = 'unresolved'
            quarters[period][symbol] = {
                'reported_shares': holding['reported_share_sum_before_overlap_resolution'],
                'positive_managers': holding['positive_reporting_managers'],
                'quality': quality_summary(holding), 'official_security_status': status,
                'official_security': official, 'separate_original_review_used': symbol == 'CJMB'}
            ledgers[(period, symbol)] = holding
    before, after = protocol['holdings_periods']; results = []
    for target in protocol['symbols']:
        symbol = target['symbol']; a, b = quarters[before][symbol], quarters[after][symbol]
        change = observed_change(ledgers[(before, symbol)], ledgers[(after, symbol)])
        results.append({'symbol': symbol, 'cusip': target['cusip'], 'previous': a, 'current': b,
                        'security_lists_match': all(q['official_security_status'] == 'matched' for q in [a,b]),
                        'strict_quality_clear': all(q['quality']['no_detected_aggregation_issues'] for q in [a,b]),
                        **change})
    report = {'version': 'batch-holdings-0.1', 'protocol_sha256': PROTOCOL_SHA,
              'periods': protocol['holdings_periods'], 'outcome_as_of': protocol['outcome_as_of'],
              'archives': sources, 'cjmb_review_sha256': COMPARISON_SHA,
              'rows': results, 'summary': {'symbols': len(results),
                'information_rows_scanned': sum(d['information_rows'] for d in datasets),
                'official_security_unresolved': sum(not r['security_lists_match'] for r in results),
                'strict_quality_clear': sum(r['strict_quality_clear'] for r in results),
                'network_requests': 0}}
    out = root / 'diagnostics/batch'; out.mkdir(parents=True, exist_ok=True)
    (out / 'holdings.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    # 詳細も保存するが、公開側は必要な集計値と保留件数に絞る。
    (out / 'holdings-ledgers.json').write_text(json.dumps({p+'/'+s:v for (p,s),v in ledgers.items()}, ensure_ascii=False))
    return report


if __name__ == '__main__':
    print(json.dumps(run(Path(__file__).resolve().parents[1])['summary'], indent=2))
