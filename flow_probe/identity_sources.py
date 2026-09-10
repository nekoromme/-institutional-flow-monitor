"""会社名の候補を原提出書類で確かめる。保有比率や検出成績の認定はしない。"""
import argparse
import hashlib
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import threading
from xml.etree import ElementTree as ET

from .bulk13f import digest
from .cohort import AS_OF
from .http_client import SafeHttp, ProbeError
from .identity_reference import HTML_REMOVAL_REVIEWS

VERSION = 'identity-sources-0.3.1'


def local(tag):
    return tag.rsplit('}', 1)[-1]


def fields(element, name):
    return [''.join(e.itertext()).strip() for e in element.iter() if local(e.tag) == name]


def single(element, name):
    values = fields(element, name)
    return values[0] if len(values) == 1 else None


def source_header(body, source):
    """索引の提出日・提出番号・書類種別を原データと照合。時刻の境界も越えない。"""
    text = body.decode('utf-8', errors='replace')
    header = text.split('</SEC-HEADER>', 1)[0]
    def value(label):
        match = re.search(r'^' + re.escape(label) + r':\s*([^\r\n]+)', header, re.M)
        plain = match.group(1).strip() if match else None
        tags = {'ACCESSION NUMBER': 'ACCESSION-NUMBER', 'CONFORMED SUBMISSION TYPE': 'TYPE',
                'FILED AS OF DATE': 'FILING-DATE'}
        tagged = re.findall(r'^<' + tags[label] + r'>([^\r\n]+)', header, re.M)
        if len(tagged) > 1 or (plain and tagged and plain != tagged[0].strip()):
            return None
        return plain or (tagged[0].strip() if tagged else None)
    accession = source['filename'].split('/')[-1].removesuffix('.txt')
    filed = value('FILED AS OF DATE')
    accepted = re.search(r'<ACCEPTANCE-DATETIME>(\d{14})', header)
    if (value('ACCESSION NUMBER') != accession or value('CONFORMED SUBMISSION TYPE') != source['form']
            or filed != source['filed'].replace('-', '') or source['filed'] >= AS_OF
            or not accepted or accepted.group(1)[:8] >= AS_OF.replace('-', '')):
        raise ValueError('original_header_or_time_mismatch')
    # 型が同じでもあり得ない日付を通さない。
    datetime.strptime(accepted.group(1), '%Y%m%d%H%M%S')
    return header, accepted.group(1)


def source_documents(body, source):
    header, accepted = source_header(body, source)
    text = body.decode('utf-8', errors='replace')
    docs = []
    for part in re.findall(r'<DOCUMENT>(.*?)</DOCUMENT>', text, flags=re.S):
        kind = re.search(r'<TYPE>([^\r\n]+)', part)
        if kind and kind.group(1).strip() == source['form']:
            docs.append(part)
    if len(docs) != 1:
        raise ValueError('ambiguous_primary_document')
    return header, docs[0], accepted


def xml_primary(doc):
    blocks = re.findall(r'<XML>\s*(.*?)\s*</XML>', doc, flags=re.S)
    if len(blocks) != 1:
        raise ValueError('missing_or_ambiguous_primary_xml')
    return ET.fromstring(blocks[0])


def ownership_identity(body, source, cik, cusip):
    _, doc, accepted = source_documents(body, source)
    root = xml_primary(doc)
    issuers = [e for e in root.iter() if local(e.tag) == 'issuerInfo']
    if len(issuers) != 1:
        raise ValueError('ambiguous_issuer_info')
    issuer = issuers[0]
    # Schedule 13Dと13Gでは正式な項目名の大文字・小文字が異なる。
    cik_values = fields(issuer, 'issuerCik') + fields(issuer, 'issuerCIK')
    cusip_values = fields(issuer, 'issuerCusip') + fields(issuer, 'issuerCUSIP')
    if len(cik_values) != 1 or len(cusip_values) != 1 or not cik_values[0].isdigit():
        raise ValueError('missing_or_ambiguous_issuer_identifiers')
    reported_cik, reported_cusip = cik_values[0].zfill(10), cusip_values[0].upper()
    matched = reported_cik == cik and reported_cusip == cusip
    return {'kind': 'ownership_identity', 'status': 'matched' if matched else 'identity_conflict',
            'issuer_cik': reported_cik, 'issuer_cusip': reported_cusip,
            'issuer_name': single(issuer, 'issuerName'),
            'security_class': single(root, 'securitiesClassTitle'), 'accepted_at_new_york': accepted,
            'uses_reporting_person_as_issuer': False}


class CoverFacts(HTMLParser):
    """表紙の文字情報だけを取り出す。同じ文脈の株式種類・銘柄・市場を組にする。"""
    wanted = {'entitycentralindexkey', 'security12btitle', 'tradingsymbol', 'securityexchangename'}

    def __init__(self):
        super().__init__()
        self.active, self.facts = None, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        name = attrs.get('name', '').split(':')[-1].lower()
        if tag == 'ix:nonnumeric' and name in self.wanted:
            if self.active is not None:
                raise ValueError('nested_cover_fact')
            self.active = {'name': name, 'context': attrs.get('contextref'), 'text': []}

    def handle_data(self, data):
        if self.active is not None:
            self.active['text'].append(data)

    def handle_endtag(self, tag):
        if tag == 'ix:nonnumeric' and self.active is not None:
            item = self.active
            item['text'] = ' '.join(''.join(item['text']).split())
            self.facts.append(item)
            self.active = None


def common_title(title):
    # 「Class A common stock」のような種類名が先頭に付く普通株も読む。
    # 優先株・預託証券・新株予約権の除外は、この後も維持する。
    normalized = re.sub(r'^CLASS\s+(?:[A-Z]|\d+)\s+', '', title.upper().strip())
    value = re.sub(r'[^A-Z]', '', normalized)
    return value.startswith(('COMMONSTOCK', 'COMMONSHARES', 'ORDINARYSHARES')) and not any(
        word in value for word in ('WARRANT', 'DEPOSITARY', 'PREFERRED'))


def listing_cover(body, source, cik):
    _, doc, accepted = source_documents(body, source)
    return parse_cover(doc, cik, accepted)


def listing_cover_parts(primary, header, source, cik):
    _, accepted = source_header(header, source)
    return parse_cover(primary.decode('utf-8', errors='replace'), cik, accepted)


def parse_cover(doc, cik, accepted):
    parser = CoverFacts(); parser.feed(doc)
    found_ciks = {r['text'].zfill(10) for r in parser.facts if r['name'] == 'entitycentralindexkey'}
    if cik not in found_ciks:
        raise ValueError('cover_issuer_cik_mismatch')
    # 親子会社の共同決算書では、同じ書類に複数の発行会社番号がある。
    # その場合は、株式種類・銘柄・市場と同じ文脈にある会社番号で限定する。
    # 文脈の対応が不明なら、先頭の会社や銘柄を勝手に採用しない。
    issuers_by_context = defaultdict(set)
    for fact in parser.facts:
        if fact['name'] == 'entitycentralindexkey':
            issuers_by_context[fact['context']].add(fact['text'].zfill(10))
    multiple_issuers = len(found_ciks) > 1
    grouped = defaultdict(lambda: defaultdict(list))
    for fact in parser.facts:
        if fact['name'] != 'entitycentralindexkey':
            grouped[fact['context']][fact['name']].append(fact['text'])
    securities, ambiguous = [], False
    names = ('security12btitle', 'tradingsymbol', 'securityexchangename')
    for context, values in grouped.items():
        if multiple_issuers:
            owners = issuers_by_context.get(context, set())
            if len(owners) == 1 and cik not in owners:
                continue
            if owners != {cik}:
                ambiguous = True
                continue
        if not context or any(len(values[k]) != 1 for k in names):
            ambiguous = True
            continue
        title, ticker, exchange = [values[k][0] for k in names]
        securities.append({'title': title, 'ticker': ticker, 'exchange': exchange, 'context': context,
                           'common_stock_candidate': common_title(title)})
    common = [s for s in securities if s['common_stock_candidate']]
    match = len(common) == 1 and not ambiguous and common[0]['ticker'] not in {'', 'N/A', 'NONE', 'None'}
    return {'kind': 'listing_cover', 'status': 'single_common_security_in_filing' if match else 'needs_class_review',
            'securities': securities, 'ticker_in_filing': common[0]['ticker'] if match else None,
            'accepted_at_new_york': accepted, 'listing_at_selection_date_confirmed': False,
            'issuer_binding': 'same_context_as_issuer_cik' if multiple_issuers else 'single_issuer_in_filing'}


def removal_notice(body, source, cik):
    header, doc, accepted = source_documents(body, source)
    accession = source['filename'].split('/')[-1].removesuffix('.txt')
    checked = HTML_REMOVAL_REVIEWS.get(accession)
    if checked is not None:
        # 原文を点検済みの2件だけを補完する。書類の内容が変われば再確認へ戻す。
        if (source['form'] != '25' or checked['issuer_cik'] != cik
                or hashlib.sha256(body).hexdigest() != checked['sha256']
                or re.findall(r'CENTRAL INDEX KEY:\s*(\d+)', header) != [cik]):
            raise ValueError('reviewed_html_removal_source_changed')
        return {'kind': 'removal', 'status': 'common_stock_notice',
                'security_class': checked['security_class'],
                'accepted_at_new_york': accepted, 'automatic_delisting_effective_date': None,
                'acquisition': 'source_bound_original_text_review',
                'reviewed_on': checked['reviewed_on']}
    root = xml_primary(doc)
    issuers = [e for e in root.iter() if local(e.tag) == 'issuer']
    if len(issuers) != 1 or single(issuers[0], 'cik') != cik:
        raise ValueError('removal_issuer_mismatch')
    title = single(root, 'descriptionClassSecurity')
    if title is None:
        raise ValueError('missing_removal_security_class')
    return {'kind': 'removal', 'status': 'common_stock_notice' if common_title(title) else 'other_or_unclassified_security_notice',
            'security_class': title, 'accepted_at_new_york': accepted,
            'automatic_delisting_effective_date': None}


def fund_report(body, source, cik):
    header, _, accepted = source_documents(body, source)
    filer = header.split('FILER:', 1)[-1]
    match = re.search(r'CENTRAL INDEX KEY:\s*(\d+)', filer)
    if not match or match.group(1).zfill(10) != cik:
        raise ValueError('fund_report_issuer_mismatch')
    return {'kind': 'fund_report', 'status': 'candidate_entity_fund_report_confirmed',
            'accepted_at_new_york': accepted, 'cusip_identity_verified_by_this_report': False}


def download_sources(root, proposals, limit=20):
    """固定順の先頭だけを深く確認。上場廃止届は他の候補分も優先して点検する。"""
    queue = {}
    for row in proposals['rows']:
        for kind, source in row['selected_sources'].items():
            if row['ordinal'] <= limit or kind == 'removal':
                queue.setdefault(source['filename'], {'source': source, 'uses': []})['uses'].append(
                    {'ordinal': row['ordinal'], 'kind': kind})
    folder = root / 'data/sec-identity/filings'; folder.mkdir(parents=True, exist_ok=True)
    prior_path = root / 'diagnostics/identity/source-download.json'
    prior = {e['source']['filename']: e for e in json.loads(prior_path.read_text())['files']} if prior_path.exists() else {}
    local_client = threading.local()
    def work(item):
        # 最大3接続、接続ごとの間隔1.2秒。原書類は鍵なしの読み取りだけ。
        if not hasattr(local_client, 'client'):
            local_client.client = SafeHttp(interval=1.2, timeout=25, max_requests=90, max_seconds=1200)
        source = item['source']; url = 'https://www.sec.gov/Archives/' + source['filename']
        path = folder / source['filename'].split('/')[-1]
        previous = prior.get(source['filename'])
        # 自分で設けた1回分の時間・件数制限は、次の明示的な取得で再開できる。
        # 提供元のアクセス拒否や容量超過まで繰り返し要求する変更ではない。
        resuming_budget = (previous and previous['status'] == 'blocked'
                           and previous.get('error', {}).get('category') == 'request_or_time_budget_exceeded')
        if previous and previous['status'] == 'blocked' and not resuming_budget:
            return {**previous, **item}
        if previous and path.exists() and digest(path) != previous.get('sha256'):
            raise ValueError('cached_original_changed')
        try:
            if not path.exists():
                path.write_bytes(local_client.client.read(url, max_bytes=24_000_000))
            return {**item, 'status': 'downloaded', 'url': url,
                    **({'resumed_after': 'request_or_time_budget_exceeded'} if resuming_budget else {}),
                    'path': str(path.relative_to(root)), 'sha256': digest(path), 'bytes': path.stat().st_size}
        except ProbeError as exc:
            return {**item, 'status': 'blocked', 'url': url, 'error': exc.summary()}
    results = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        for future in as_completed([pool.submit(work, item) for item in queue.values()]):
            results.append(future.result())
            report = {'scope': f'first_{limit}_in_frozen_order_plus_latest_indexed_removal_per_issuer',
                      'planned_files': len(queue), 'completed_files': len(results),
                      'files': sorted(results, key=lambda x: x['url'])}
            (root / 'diagnostics/identity/source-download.json').write_text(json.dumps(report, indent=2) + '\n')
            print(json.dumps({'done': len(results), 'total': len(queue), 'status': results[-1]['status']}), flush=True)
    return report


def fetch_primary_covers(root, proposals, downloads):
    """添付資料を含む全文が容量上限を超えた時だけ、本文と原ヘッダーを個別取得する。

    サーバーのアクセス拒否を回避する処理ではない。本文の所在は同じ提出番号の
    公的メタデータから得て、提出日・書類種別を検証してから読む。
    """
    lookup = {r['ordinal']: r for r in proposals['rows']}
    targets = sorted({u['ordinal'] for e in downloads['files'] if e['status'] == 'blocked'
                      and e.get('error', {}).get('category') == 'response_too_large'
                      for u in e['uses'] if u['kind'] == 'listing_cover'})
    out = root / 'diagnostics/identity/primary-cover-download.json'
    prior = {r['ordinal']: r for r in json.loads(out.read_text())['files']} if out.exists() else {}
    def work(n):
        if n in prior:
            return prior[n]
        row = lookup[n]; source = row['selected_sources']['listing_cover']
        cik = row['issuer_candidate_cik']; accession = source['filename'].split('/')[-1].removesuffix('.txt')
        client = SafeHttp(interval=1.2, timeout=25, max_requests=10, max_seconds=160)
        try:
            meta = client.json(f'https://data.sec.gov/submissions/CIK{cik}.json')
            if str(meta.get('cik')).zfill(10) != cik:
                return {'ordinal': n, 'status': 'metadata_issuer_mismatch'}
            recent = meta['filings']['recent']
            ids = [i for i, a in enumerate(recent['accessionNumber']) if a == accession]
            if len(ids) != 1:
                return {'ordinal': n, 'status': 'metadata_missing'}
            i = ids[0]
            if recent['filingDate'][i] != source['filed'] or recent['form'][i] != source['form']:
                return {'ordinal': n, 'status': 'metadata_mismatch'}
            base = f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace("-", "")}/'
            folder = root / 'data/sec-identity/primary'; folder.mkdir(parents=True, exist_ok=True)
            parts = {}
            for kind, filename in [('header', accession + '.hdr.sgml'), ('primary', recent['primaryDocument'][i])]:
                suffix = '-header.sgml' if kind == 'header' else '-primary.htm'
                path = folder / (accession + suffix)
                if not path.exists():
                    path.write_bytes(client.read(base + filename, max_bytes=16_000_000))
                parts[kind] = {'url': base + filename, 'path': str(path.relative_to(root)),
                               'sha256': digest(path), 'bytes': path.stat().st_size}
            return {'ordinal': n, 'status': 'downloaded', 'source': source, 'cik': cik, **parts}
        except ProbeError as exc:
            return {'ordinal': n, 'status': 'blocked', 'error': exc.summary()}
    results = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        for future in as_completed([pool.submit(work, n) for n in targets]):
            results.append(future.result())
            out.write_text(json.dumps({'files': sorted(results, key=lambda r: r['ordinal'])}, indent=2) + '\n')


def review(root, proposals, downloads):
    indexed = {entry['source']['filename']: entry for entry in downloads['files']}
    fallback_path = root / 'diagnostics/identity/primary-cover-download.json'
    fallbacks = {e['ordinal']: e for e in json.loads(fallback_path.read_text())['files']
                 if e['status'] == 'downloaded'} if fallback_path.exists() else {}
    rows = []
    for row in proposals['rows']:
        results = {}
        for kind, source in row['selected_sources'].items():
            entry = indexed.get(source['filename'])
            if (not entry or entry['status'] != 'downloaded') and kind == 'listing_cover' and row['ordinal'] in fallbacks:
                fallback = fallbacks[row['ordinal']]
                if fallback['source'] != source or fallback['cik'] != row['issuer_candidate_cik']:
                    raise ValueError('fallback_source_identity_changed')
                for part in ('primary', 'header'):
                    if digest(root / fallback[part]['path']) != fallback[part]['sha256']:
                        raise ValueError('fallback_source_changed')
                try:
                    result = listing_cover_parts((root / fallback['primary']['path']).read_bytes(),
                                                  (root / fallback['header']['path']).read_bytes(), source,
                                                  row['issuer_candidate_cik'])
                except ValueError as exc:
                    result = {'status': 'parse_or_validation_requires_review', 'reason': str(exc)[:100]}
                results[kind] = {**result, 'source_url': fallback['primary']['url'],
                                 'source_sha256': fallback['primary']['sha256'],
                                 'header_source_url': fallback['header']['url'],
                                 'header_source_sha256': fallback['header']['sha256'],
                                 'acquisition': 'primary_document_plus_original_header',
                                 'form': source['form'], 'filed': source['filed']}
                continue
            if not entry or entry['status'] != 'downloaded':
                results[kind] = {'status': 'not_fetched' if not entry else 'download_blocked'}
                continue
            path = root / entry['path']
            if digest(path) != entry['sha256']:
                raise ValueError('original_source_changed')
            try:
                args = (path.read_bytes(), source, row['issuer_candidate_cik'])
                if kind == 'ownership_identity':
                    result = ownership_identity(*args, row['cusip'])
                else:
                    result = {'listing_cover': listing_cover, 'removal': removal_notice, 'fund_report': fund_report}[kind](*args)
            except (ValueError, ET.ParseError) as exc:
                result = {'status': 'parse_or_validation_requires_review', 'reason': str(exc)[:100]}
            results[kind] = {**result, 'source_url': entry['url'], 'source_sha256': entry['sha256'],
                             'form': source['form'], 'filed': source['filed']}
        identity = results.get('ownership_identity', {}).get('status') == 'matched'
        cover = results.get('listing_cover', {})
        ticker = cover.get('ticker_in_filing') if identity else None
        flags = list(row['index_flags'])
        if results.get('removal', {}).get('status') == 'common_stock_notice':
            flags.append('common_stock_removal_notice_requires_resolution_before_admission')
        rows.append({'ordinal': row['ordinal'], 'cusip': row['cusip'], 'issuer': row['issuer'],
                     'issuer_candidate_cik': row['issuer_candidate_cik'], 'identity_verified': identity,
                     'ticker_in_reviewed_filing': ticker, 'ticker_at_selection_date': None,
                     'reviews': results, 'review_flags': flags, 'additional_removal_notices_not_reviewed': len(row['additional_removal_notices']),
                     'eligible_for_backtest': False})
    return {'version': VERSION, 'as_of': AS_OF, 'input_frame_sha256': proposals['input_frame_sha256'],
            'scope': downloads['scope'], 'source_files_downloaded': sum(e['status'] == 'downloaded' for e in indexed.values()),
            'source_bytes': sum(e.get('bytes', 0) for e in indexed.values()),
            'primary_document_fallback_count': len(fallbacks),
            'summary': {'candidates': len(rows), 'issuer_name_candidates': sum(r['issuer_candidate_cik'] is not None for r in rows),
                        'cusip_issuer_verified': sum(r['identity_verified'] for r in rows),
                        'ticker_linked_to_reviewed_cover': sum(r['ticker_in_reviewed_filing'] is not None for r in rows),
                        'common_stock_removal_notices': sum(r['reviews'].get('removal', {}).get('status') == 'common_stock_notice' for r in rows),
                        'fund_reports_for_candidate_entities': sum(r['reviews'].get('fund_report', {}).get('status') == 'candidate_entity_fund_report_confirmed' for r in rows),
                        'backtest_eligible': 0},
            'rows': rows}


def public_outputs(proposals, report):
    names = {k: v for k, v in proposals.items() if k != 'rows'}
    names['rows'] = [{k: v for k, v in row.items() if k not in {'indexed_form_counts', 'selected_sources'}}
                     for row in proposals['rows']]
    reviewed = {k: v for k, v in report.items() if k != 'rows'}
    reviewed['rows'] = []
    for row in report['rows']:
        item = {k: v for k, v in row.items() if k != 'reviews'}
        item['pending_sources'] = [k for k, v in row['reviews'].items() if v['status'] == 'not_fetched']
        item['reviews'] = {k: v for k, v in row['reviews'].items() if v['status'] != 'not_fetched'}
        reviewed['rows'].append(item)
    return names, reviewed


def main():
    parser = argparse.ArgumentParser(description='当時の原書類で証券番号・銘柄・登録情報を照合する')
    parser.add_argument('--fetch', action='store_true')
    parser.add_argument('--limit', type=int, default=20, choices=range(1, 201))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    proposals = json.loads((root / 'diagnostics/identity/name-candidates.json').read_text())
    downloads = download_sources(root, proposals, args.limit) if args.fetch else json.loads(
        (root / 'diagnostics/identity/source-download.json').read_text())
    if args.fetch:
        fetch_primary_covers(root, proposals, downloads)
    report = review(root, proposals, downloads)
    (root / 'diagnostics/identity/review.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    names, reviewed = public_outputs(proposals, report)
    for name, content in [('name-summary', names), ('review-summary', reviewed)]:
        (root / f'diagnostics/identity/{name}.json').write_text(json.dumps(content, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report['summary'], indent=2))


if __name__ == '__main__':
    main()
