"""当時の提出書類の索引を揃え、証券と会社を照合する次工程の入口を作る。

現在の会社名簿ではなく、その年より前の提出記録を使う。名前の類似は
原書類を読む順番を決めるためだけで、同一銘柄の証明にはしない。
"""
from collections import defaultdict
import gzip
import json
from pathlib import Path
from flow_probe.bulk13f import digest
from flow_probe.http_client import SafeHttp
from flow_probe.identity import propose
from .collect import ROOT, fetch_file, write_json


def index_records(raw, year):
    records = {}
    for line in gzip.decompress(raw).decode('latin-1').splitlines():
        fields = line.split('|')
        if len(fields)!=5 or not fields[0].isdigit():
            continue
        cik,name,form,filed,filename=fields
        # 開始年の時点より前の索引だけに固定する。今の銘柄一覧は参照しない。
        if f'{year-1}-01-01' <= filed < f'{year}-01-01':
            records[(cik.zfill(10),filename)]={'cik':cik.zfill(10),'name':name,
                'form':form,'filed':filed,'filename':filename}
    return list(records.values())


def collect_indexes(root, year, client):
    records, sources = {}, []
    for q in range(1,5):
        print(json.dumps({'progress':'filing_index','selection_year':year,'quarter':q}),flush=True)
        path=root/f'data/history/indexes/{year-1}-QTR{q}-master.gz'
        url=f'https://www.sec.gov/Archives/edgar/full-index/{year-1}/QTR{q}/master.gz'
        sources.append(fetch_file(client,path,url))
        for r in index_records(path.read_bytes(),year):
            records[(r['cik'],r['filename'])]=r
    entities, files=defaultdict(set),defaultdict(list)
    for r in records.values():
        entities[r['cik']].add(r['name']);files[r['cik']].append(r)
    destination=root/f'diagnostics/history/index-{year}-summary.json'
    result={'year':year,'selection_as_of':f'{year}-01-01','sources':sources,
            'indexed_entities':len(entities),'indexed_filings':len(records),
            'current_index_of_historical_filings_not_original_vintage':True}
    write_json(destination,result)
    return entities,files,result


def match_saved_frame(root,year,entities,files):
    source=root/f'diagnostics/history/frames/{year}-official-common.json'
    frame=json.loads(source.read_text())
    batch=[dict(r,issuer=r['reported_names'][0]) for r in frame['review_batch']]
    proposals=propose(batch,entities,files)
    result={'year':year,'selection_as_of':f'{year}-01-01','input_frame_sha256':digest(source),
            'rows':proposals,'name_match_is_identity_proof':False,'certified_for_backtest':0,
            'legacy_ownership_form_names': 'SC 13D/G forms may need explicit source selection in the next step'}
    write_json(root/f'diagnostics/history/identity-{year}-candidates.json',result)
    return {'year':year,'reviewed_names':len(proposals),
            'single_name_candidate':sum(r['issuer_candidate_cik'] is not None for r in proposals),
            'historical_listing_source_candidates':sum('listing_cover' in r['selected_sources'] for r in proposals),
            'identity_verified':0}


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--indexes-only',action='store_true')
    args=parser.parse_args()
    client=SafeHttp(timeout=90,max_requests=30,max_seconds=1200)
    reports=[]
    for year in (2023,2024,2025):
        entities,files,report=collect_indexes(ROOT,year,client)
        if not args.indexes_only:
            report['matching']=match_saved_frame(ROOT,year,entities,files)
        reports.append(report)
    write_json(ROOT/'diagnostics/history/index-collection-summary.json',{'years':reports,'http':client.metrics()})
