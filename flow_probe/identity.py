"""過去の提出者名は検索の手掛かり。証券番号との同一性は原書類で確認する。"""
import argparse
from collections import Counter, defaultdict
from difflib import SequenceMatcher
import gzip
import hashlib
import json
from pathlib import Path
import re

from .bulk13f import digest
from .cohort import AS_OF
from .holdings import _get_file
from .http_client import SafeHttp

VERSION = 'historical-identity-0.1'
INDEX_SHAS = {
    1: '7f2dce76a503d73a1f819b3221c33818689954868b9100c9aa0a506f2ed9fed1',
    2: '5d4ba07160b4356cfc0983bfbad1c7d5e5eba3b9d9f6ff1424b23b87e594dbfc',
    3: '523c9fadd1c0add56fc0af183e424ba24fb6e532c1bde03e994a6f035abe0460',
    4: '7e4f3c550d6b7eb84c85b6f277d94fbc0ba085e1aba2ba3cd8030cb9d15028d8',
}
WORDS = {'HLDG': 'HOLDINGS', 'HLDGS': 'HOLDINGS', 'HLDNG': 'HOLDINGS', 'INTL': 'INTERNATIONAL',
         'INDS': 'INDUSTRIES', 'INCORPORATED': 'INC', 'CORPORATION': 'CORP', 'CO': 'COMPANY',
         'PPTYS': 'PROPERTIES', 'FD': 'FUND', 'MUN': 'MUNICIPAL', 'MUNCPL': 'MUNICIPAL',
         'FINL': 'FINANCIAL', 'SVCS': 'SERVICES', 'SVC': 'SERVICE', 'MTN': 'MOUNTAIN',
         'QLTY': 'QUALITY', 'RTY': 'ROYALTY', 'INTST': 'INTERSTATE', 'INVT': 'INVESTMENT',
         'MGMT': 'MANAGEMENT', 'RES': 'RESOURCES', 'SYS': 'SYSTEMS', 'TECHNLGY': 'TECHNOLOGY',
         'CAP': 'CAPITAL', 'HI': 'HIGH', 'YLD': 'YIELD', 'DIVID': 'DIVIDEND', 'FIN': 'FINANCE', 'TRANS': 'TRANSPORT', 'AIRLS': 'AIRLINES', 'MATLS': 'MATERIALS',
         'STD': 'STANDARD', 'ENTMT': 'ENTERTAINMENT', 'INS': 'INSURANCE', 'SELIGM': 'SELIGMAN',
         'PREM': 'PREMIUM', 'TECH': 'TECHNOLOGY', 'GR': 'GROWTH', 'MTG': 'MORTGAGE',
         'COS': 'COMPANIES', 'TR': 'TRUST', 'PETE': 'PETROLEUM', 'PAC': 'PACIFIC', 'WTR': 'WATER'}


def name_key(name):
    # 省略を展開するのは照会先候補のためだけ。これを同一証券の証拠にはしない。
    name = re.sub(r'/[^/]{1,6}/?$', '', name.upper())
    words = re.findall(r'[A-Z0-9]+', name.replace('&', ' AND '))
    words = [WORDS.get(w, w) for w in words if w != 'THE']
    while words and words[-1] in {'INC', 'CORP', 'COMPANY', 'NEW', 'MASS', 'IND', 'N', 'COM'}:
        words.pop()
    return ''.join(words)


def read_indexes(root):
    entities, files, manifests = defaultdict(set), {}, []
    for q, checksum in INDEX_SHAS.items():
        path = root / f'data/sec-identity/2025-QTR{q}-master.gz'
        if digest(path) != checksum:
            raise ValueError('historical_filing_index_changed')
        for line in gzip.decompress(path.read_bytes()).decode('latin-1').splitlines():
            parts = line.split('|')
            if len(parts) != 5 or not parts[0].isdigit():
                continue
            cik, name, form, filed, filename = parts
            if not '2025-01-01' <= filed < AS_OF:
                continue
            cik = cik.zfill(10)
            entities[cik].add(name)
            # 同じ書類が提出者と対象会社の両方に索引化されることがある。
            files[(cik, filename)] = {'cik': cik, 'name': name, 'form': form, 'filed': filed, 'filename': filename}
        manifests.append({'url': f'https://www.sec.gov/Archives/edgar/full-index/2025/QTR{q}/master.gz',
                          'sha256': checksum, 'bytes': path.stat().st_size})
    by_cik = defaultdict(list)
    for row in files.values():
        by_cik[row['cik']].append(row)
    return entities, by_cik, manifests


def propose(batch, entities, files):
    buckets = defaultdict(list)
    for cik, names in entities.items():
        for name in names:
            key = name_key(name)
            if key:
                buckets[key[:4]].append((key, cik, name))
    output = []
    for i, item in enumerate(batch, 1):
        key = name_key(item['issuer'])
        options = {}
        for other, cik, name in buckets[key[:4]]:
            value = (1.0 if key == other else
                     .96 if len(key) >= 12 and other.startswith(key) else
                     SequenceMatcher(None, key, other).ratio())
            if value >= .70 and (cik not in options or value > options[cik]['name_similarity']):
                options[cik] = {'cik': cik, 'name': name, 'name_similarity': round(value, 4)}
        ranked = sorted(options.values(), key=lambda r: (-r['name_similarity'], r['cik']))[:3]
        confident = bool(ranked and ranked[0]['name_similarity'] >= .90 and
                         (len(ranked) == 1 or ranked[0]['name_similarity'] - ranked[1]['name_similarity'] >= .08))
        chosen = ranked[0]['cik'] if confident else None
        indexed = files.get(chosen, [])
        forms = Counter(r['form'] for r in indexed)
        flags = []
        if any(f.startswith(('N-CSR', 'NPORT', 'N-CEN')) for f in forms):
            flags.append('investment_fund_forms_indexed_requires_entity_review')
        if any(f.startswith(('25', '15-')) for f in forms):
            flags.append('registration_removal_or_termination_indexed_requires_security_review')
        if any(f.startswith(('20-F', '40-F')) for f in forms):
            flags.append('foreign_issuer_forms_indexed_requires_scope_review')
        sources = {}
        for kind, allowed in [('ownership_identity', {'SCHEDULE 13G','SCHEDULE 13G/A','SCHEDULE 13D','SCHEDULE 13D/A'}),
                              ('listing_cover', {'10-Q','10-K','20-F','40-F'}),
                              ('removal', {'25-NSE','25','25/A'}),
                              ('fund_report', {'N-CSR','N-CSRS'})]:
            eligible = [r for r in indexed if r['form'] in allowed]
            if eligible:
                sources[kind] = sorted(eligible, key=lambda r: (r['filed'], r['filename']))[-1]
        output.append({'ordinal': i, 'cusip': item['cusip'], 'issuer': item['issuer'],
                       'issuer_candidate_cik': chosen, 'name_candidates': ranked,
                       'name_match_is_identity_proof': False, 'index_flags': flags,
                       'indexed_form_counts': dict(sorted(forms.items())), 'selected_sources': sources,
                       'additional_removal_notices': sorted(
                           [r for r in indexed if r['form'] in {'25-NSE','25','25/A'} and r != sources.get('removal')],
                           key=lambda r: (r['filed'], r['filename'])),
                       'identity_verified': False, 'eligible_for_backtest': False})
    return output


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description='200件を過去の提出者名へ照合する。名前の一致では証券を認定しない。')
    parser.add_argument('--fetch', action='store_true', help='不足する2025年の四半期索引を取得する。合計約15MB。')
    args = parser.parse_args()
    if args.fetch:
        client = SafeHttp(timeout=25, max_requests=12, max_seconds=240)
        for q, checksum in INDEX_SHAS.items():
            _get_file(client, root / f'data/sec-identity/2025-QTR{q}-master.gz',
                      f'https://www.sec.gov/Archives/edgar/full-index/2025/QTR{q}/master.gz', checksum,
                      max_bytes=25_000_000)
    frame_path = root / 'docs/evidence/historical-frame-2026-09-10.json'
    batch = json.loads(frame_path.read_text())['review_batch']
    entities, files, sources = read_indexes(root)
    rows = propose(batch, entities, files)
    out = root / 'diagnostics/identity'
    out.mkdir(parents=True, exist_ok=True)
    report = {'version': VERSION, 'as_of': AS_OF, 'input_frame_sha256': digest(frame_path),
              'index_sources': sources, 'indexed_entities': len(entities),
              'index_vintage': 'current_reconstruction_of_2025_indexes; post_acceptance_corrections_possible',
              'rows': rows}
    (out / 'name-candidates.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'total': len(rows), 'single_candidate': sum(r['issuer_candidate_cik'] is not None for r in rows),
                      'flags': dict(Counter(f for r in rows for f in r['index_flags']))}, indent=2))


if __name__ == '__main__':
    main()
