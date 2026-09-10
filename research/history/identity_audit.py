"""年別の証券候補を原書類に結び付ける。成績や保有比率は計算しない。

名前は照会先を探す手掛かりにすぎない。証券番号と発行会社の原文照合を
通るまで銘柄コードを価格取得の対象に昇格させない。
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import gzip
import hashlib
from html.parser import HTMLParser
import json
import re
import threading
import time
import unicodedata
from pathlib import Path

from flow_probe.bulk13f import digest
from flow_probe.http_client import SafeHttp, ProbeError
from flow_probe.identity import propose
from flow_probe.identity_sources import source_documents, parse_cover
from .collect import ROOT, write_json
from .indexes import collect_indexes
from .security_lists import parse_list

YEARS=(2023,2024,2025)
OWNERSHIP_FORMS={'SC 13D','SC 13D/A','SC 13G','SC 13G/A'}


def official_names(text, year):
    # 全行数・日付・株式区分の整合性は共通の読み取り処理で先に検査する。
    allowed,_=parse_list(text,year)
    names={}
    for line in text.splitlines():
        prefix=re.match(r'^([A-Z0-9]{6})\s+([A-Z0-9]{2})\s+(\d)\s+(?:\*\s*)?(.*)$',line)
        if not prefix:continue
        cusip=prefix[1]+prefix[2]+prefix[3]
        if not allowed.get(cusip):continue
        suffix=re.search(r'\s{2,}COM(?: SHS)?(?:\s+ADDED)?\s*$',line)
        if not suffix:raise ValueError('official_common_name_not_parsed')
        name=' '.join(line[prefix.start(4):suffix.start()].split())
        if not name:raise ValueError('official_issuer_name_empty')
        if cusip in names and names[cusip]!=name:raise ValueError('official_issuer_name_conflict')
        names[cusip]=name
    return names


def prepare(root):
    reports=[]
    client=SafeHttp(timeout=90,max_requests=20,max_seconds=1200)
    for year in YEARS:
        framepath=root/f'diagnostics/history/frames/{year}-official-common.json'
        frame=json.loads(framepath.read_text())
        names=official_names((root/f'data/history/security-lists/{year-1}q3.layout.txt').read_text(),year-1)
        batch=[dict(r,issuer=names[r['cusip']]) for r in frame['review_batch']]
        entities,files,_=collect_indexes(root,year,client)
        rows=propose(batch,entities,files)
        old=json.loads((root/f'diagnostics/history/identity-{year}-candidates.json').read_text())
        old_by_id={r['cusip']:r for r in old['rows']}
        for row in rows:
            previous=old_by_id[row['cusip']]
            row['prior_candidate_cik']=previous['issuer_candidate_cik']
            row['candidate_cik_changed']=previous['issuer_candidate_cik']!=row['issuer_candidate_cik']
            row['lookup_name_source']='same_CUSIP_in_historical_official_security_list'
            indexed=files.get(row['issuer_candidate_cik'],[])
            ownership=[r for r in indexed if r['form'] in OWNERSHIP_FORMS]
            if ownership:
                row['selected_sources']['ownership_identity']=max(ownership,key=lambda r:(r['filed'],r['filename']))
        report={'year':year,'as_of':f'{year}-01-01','input_frame_sha256':digest(framepath),
                'official_list_sha256':digest(root/f'data/history/security-lists/{year-1}q3.pdf'),
                'name_match_is_identity_proof':False,'rows':rows}
        # 保存版はSEC報告の会社名を使う。公式一覧の会社名の一括転載を避ける。
        reported={r['cusip']:r['reported_names'] for r in frame['review_batch']}
        for r in rows:r['issuer']=reported[r['cusip']][0]
        write_json(root/f'diagnostics/history/audit/proposals-{year}.json',report)
        reports.append(report)
        print(json.dumps({'year':year,'candidates':len(rows),
            'lookup_candidate_changes':sum(r['candidate_cik_changed'] for r in rows),
            'issuer_candidates':sum(r['issuer_candidate_cik'] is not None for r in rows),
            'ownership_sources':sum('ownership_identity' in r['selected_sources'] for r in rows)}),flush=True)
    return reports


class VisibleText(HTMLParser):
    """表紙の文字列を順に読む。スクリプトや装飾の文字列は証拠にしない。"""
    def __init__(self):super().__init__();self.parts=[];self.hidden=0
    def handle_starttag(self,tag,attrs):
        if tag in {'script','style'}:self.hidden+=1
    def handle_endtag(self,tag):
        if tag in {'script','style'}:self.hidden=max(0,self.hidden-1)
    def handle_data(self,data):
        if not self.hidden:self.parts.append(data)


def legacy_ownership(body,source,cik,cusip,as_of):
    header,doc,accepted=source_documents(body,source,as_of=as_of)
    # 提出者ではなく「保有されている会社」の番号だけを採用する。
    subjects=re.findall(r'SUBJECT COMPANY:(.*?)(?=\n(?:FILED BY|FILER|SUBJECT COMPANY):|\Z)',header,re.S)
    ids=[v.zfill(10) for part in subjects for v in re.findall(r'CENTRAL INDEX KEY:\s*(\d+)',part)]
    if ids!=[cik]:return {'status':'subject_company_mismatch','subject_ciks':ids}
    parser=VisibleText();parser.feed(doc)
    visible=' '.join(parser.parts)
    visible=''.join(ch for ch in visible if unicodedata.category(ch)!='Cf')
    text=' '.join(visible.split())
    # 番号の直後に括弧付きの欄名がある形と、欄名の直後に番号がある形。
    # 本文の保有者番号や日付のどこかに一致しただけでは通さない。
    found=[]
    number=r'([A-Z0-9]{6}[ -]?[A-Z0-9]{2}[ -]?\d)'
    patterns=[r'(?<![A-Z0-9])'+number+r'\s*\(\s*CUSIP(?:\s+(?:NUMBER|NO\.?))?\s*\)',
              r'\bCUSIP(?:\s+(?:NUMBER|NO\.?))?\s*[:#]?\s*'+number+r'(?![A-Z0-9])']
    for pattern in patterns:
        for m in re.finditer(pattern,text[:16000],re.I):
            found.append(re.sub(r'[^A-Z0-9]','',m[1].upper()))
    unique=sorted(set(found))
    matched=unique==[cusip]
    return {'status':'matched' if matched else 'cusip_cover_requires_review',
            'subject_ciks':ids,'cusips_in_cover_fields':unique,
            'accepted_at_new_york':accepted,'uses_reporting_person_as_issuer':False}


def queue_sources(proposals,limit=40):
    queue={}
    for report in proposals:
        for row in report['rows']:
            for kind,source in row['selected_sources'].items():
                if kind not in {'ownership_identity','listing_cover','removal'}:continue
                if row['ordinal']>limit and kind!='removal':continue
                queue.setdefault(source['filename'],{'source':source,'uses':[]})['uses'].append(
                    {'year':report['year'],'ordinal':row['ordinal'],'kind':kind})
    return queue


def download(root,proposals,limit=40):
    queue=queue_sources(proposals,limit)
    out=root/'diagnostics/history/audit/downloads.json'
    prior={r['source']['filename']:r for r in json.loads(out.read_text())['files']} if out.exists() else {}
    folder=root/'data/history/identity-originals';folder.mkdir(parents=True,exist_ok=True)
    state=threading.local()
    # 応答待ちが長いため同時接続を8本まで許すが、開始は全接続で0.4秒間隔。
    # 接続ごとに独立して連打する方式にはしない。
    throttle=threading.Lock()
    last_start=[0.0]
    def work(item):
        source=item['source'];old=prior.get(source['filename'])
        if old and old['status']=='downloaded':
            path=root/old['path'];raw=gzip.decompress(path.read_bytes())
            if hashlib.sha256(raw).hexdigest()!=old['sha256']:raise ValueError('cached_original_changed')
            return {**old,**item}
        if old and old.get('error',{}).get('category') not in {'request_or_time_budget_exceeded','network_or_timeout'}:
            return {**old,**item}
        if not hasattr(state,'client'):
            state.client=SafeHttp(interval=1.2,timeout=30,max_requests=500,max_seconds=1800)
        url='https://www.sec.gov/Archives/'+source['filename']
        path=folder/(source['filename'].split('/')[-1]+'.gz')
        try:
            with throttle:
                time.sleep(max(0,0.4-(time.monotonic()-last_start[0])))
                last_start[0]=time.monotonic()
            raw=state.client.read(url,max_bytes=24_000_000)
            compressed=gzip.compress(raw,mtime=0)
            temp=path.with_suffix('.part');temp.write_bytes(compressed);temp.replace(path)
            return {**item,'status':'downloaded','url':url,'path':str(path.relative_to(root)),
                    'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'compressed_bytes':len(compressed)}
        except ProbeError as exc:return {**item,'status':'blocked','url':url,'error':exc.summary()}
    results=[]
    with ThreadPoolExecutor(max_workers=8) as pool:
        for future in as_completed([pool.submit(work,item) for item in queue.values()]):
            results.append(future.result())
            write_json(out,{'planned_files':len(queue),'files':sorted(results,key=lambda r:r['source']['filename'])})
            if len(results)%20==0 or len(results)==len(queue):
                print(json.dumps({'completed':len(results),'planned':len(queue),
                     'statuses':dict(Counter(r['status'] for r in results))}),flush=True)
    return {'files':results}


def review(root,proposals,downloads):
    indexed={r['source']['filename']:r for r in downloads['files']}
    reports=[]
    for report in proposals:
        rows=[]
        for row in report['rows']:
            results={}
            for kind,source in row['selected_sources'].items():
                entry=indexed.get(source['filename'])
                if not entry or entry['status']!='downloaded':
                    results[kind]={'status':'not_downloaded','error':entry.get('error') if entry else None};continue
                raw=gzip.decompress((root/entry['path']).read_bytes())
                if hashlib.sha256(raw).hexdigest()!=entry['sha256']:raise ValueError('original_hash_mismatch')
                try:
                    if kind=='ownership_identity':result=legacy_ownership(raw,source,row['issuer_candidate_cik'],row['cusip'],report['as_of'])
                    else:
                        _,doc,accepted=source_documents(raw,source,as_of=report['as_of'])
                        result=(parse_cover(doc,row['issuer_candidate_cik'],accepted) if kind=='listing_cover'
                                else {'status':'removal_filing_header_verified_requires_class_and_effective_date_review','accepted_at_new_york':accepted})
                except (ValueError,UnicodeError) as exc:result={'status':'parse_or_validation_requires_review','reason':str(exc)[:100]}
                results[kind]={**result,'source_url':entry['url'],'source_sha256':entry['sha256'],'filed':source['filed'],'form':source['form']}
            identity=results.get('ownership_identity',{}).get('status')=='matched'
            ticker=results.get('listing_cover',{}).get('ticker_in_filing') if identity else None
            rows.append({'year':report['year'],'ordinal':row['ordinal'],'cusip':row['cusip'],
                'issuer_candidate_cik':row['issuer_candidate_cik'],'candidate_cik_changed':row['candidate_cik_changed'],
                'cusip_issuer_verified':identity,'ticker_in_reviewed_filing':ticker,
                'price_collection_candidate':bool(ticker) and not row['index_flags'],
                'index_flags':row['index_flags'],'additional_removal_notices_not_reviewed':len(row['additional_removal_notices']),
                'listing_at_selection_date_confirmed':False,'backtest_eligible':False,'reviews':results})
        summary={'year':report['year'],'candidates':len(rows),'cusip_issuer_verified':sum(r['cusip_issuer_verified'] for r in rows),
            'ticker_linked':sum(bool(r['ticker_in_reviewed_filing']) for r in rows),
            'price_collection_candidates':sum(r['price_collection_candidate'] for r in rows),
            'backtest_eligible':0,'lookup_candidate_changes':sum(r['candidate_cik_changed'] for r in rows),
            'ownership_statuses':dict(Counter(r['reviews'].get('ownership_identity',{}).get('status','no_source') for r in rows)),
            'cover_statuses':dict(Counter(r['reviews'].get('listing_cover',{}).get('status','no_source') for r in rows))}
        result={'summary':summary,'rows':rows,'as_of':report['as_of'],'returns_computed':False}
        write_json(root/f'diagnostics/history/audit/review-{report["year"]}.json',result)
        print(json.dumps(summary),flush=True);reports.append(result)
    return reports


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--prepare',action='store_true');parser.add_argument('--fetch',action='store_true')
    parser.add_argument('--limit',type=int,default=40,choices=range(1,201));args=parser.parse_args()
    proposals=prepare(ROOT) if args.prepare else [json.loads((ROOT/f'diagnostics/history/audit/proposals-{y}.json').read_text()) for y in YEARS]
    downloads=download(ROOT,proposals,args.limit) if args.fetch else json.loads((ROOT/'diagnostics/history/audit/downloads.json').read_text())
    review(ROOT,proposals,downloads)


if __name__=='__main__':main()
