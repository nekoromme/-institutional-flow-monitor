"""公式の四半期証券一覧と報告上の候補を突き合わせる。

PDFの全行数を末尾の総件数と照合する。オプション行などの同じ証券番号が
複数行あっても、株式区分の判断が矛盾したら停止する。
"""
from datetime import datetime
import json
import re
import subprocess
from flow_probe.bulk13f import digest
from flow_probe.http_client import SafeHttp
from .collect import ROOT, fetch_file, stable_batch, write_json


def parse_list(text, year):
    if not re.search(rf'Year:\s+{year}\s+Qtr:\s+3',text):
        raise ValueError('official_list_period_mismatch')
    totals=re.findall(r'Total Count:\s*([\d,]+)',text)
    if len(totals)!=1:raise ValueError('official_list_total_missing')
    run_dates=set(re.findall(r'Run Date:\s*(\d+/\d+/\d+)',text))
    if len(run_dates)!=1:raise ValueError('official_list_run_date_ambiguous')
    run_date=datetime.strptime(next(iter(run_dates)),'%m/%d/%Y').date().isoformat()
    if not f'{year}-09-01' <= run_date < f'{year+1}-01-01':
        raise ValueError('official_list_not_before_cutoff')
    rows={};count=0
    for line in text.splitlines():
        m=re.match(r'^([A-Z0-9]{6})\s+([A-Z0-9]{2})\s+(\d)\s+(?:\*\s*)?(.*)$',line)
        if not m:continue
        count+=1;cusip=m[1]+m[2]+m[3]
        # 記号*はオプション取引可能の印。普通株自体をオプションと誤分類しない。
        c=re.search(r'\s{2,}(COM(?: SHS)?)(?:\s+(ADDED|DELETED))?\s*$',line)
        ordinary=bool(c and c[2]!='DELETED')
        if cusip in rows and rows[cusip]!=ordinary:
            raise ValueError('official_list_conflicting_security_class')
        rows[cusip]=ordinary
    if count!=int(totals[0].replace(',','')):
        raise ValueError('official_list_incomplete_parse')
    return rows,{'rows':count,'unique_security_numbers':len(rows),
                 'repeated_security_numbers':count-len(rows),'narrow_common_unique':sum(rows.values()),
                 'run_date':run_date,'total_count_verified':True,
                 'historical_publication_timestamp_verified':False}


def build(root, year):
    path=root/f'data/history/security-lists/{year-1}q3.pdf'
    text_path=path.with_suffix('.layout.txt')
    subprocess.run(['pdftotext','-layout',str(path),str(text_path)],check=True)
    securities,stats=parse_list(text_path.read_text(),year-1)
    source=root/f'diagnostics/history/frames/{year}.json'
    original=json.loads(source.read_text())
    frame=[r for r in original['frame'] if securities.get(r['cusip']) is True]
    destination=root/f'diagnostics/history/frames/{year}-official-common.json'
    output={'selection_as_of':original['selection_as_of'],'holdings_period':original['holdings_period'],
            'source_frame_sha256':digest(source),'official_list_sha256':digest(path),
            'frame':frame,'review_batch':stable_batch(frame),'seed':original['seed'],
            'certified_for_backtest':False}
    write_json(destination,output)
    report={'year':year,'source':json.loads(path.with_suffix('.pdf.source.json').read_text()),
            'pdf_table_validation':stats,'reported_frame_count':len(original['frame']),
            'official_common_and_reported_count':len(frame),'review_batch_count':len(output['review_batch']),
            'frame_sha256':digest(destination),'historical_identity_and_low_ownership_verified':False}
    write_json(root/f'diagnostics/history/security-list-{year}-summary.json',report)
    return report


if __name__=='__main__':
    client=SafeHttp(timeout=90,max_requests=6,max_seconds=600)
    for year in (2023,2024,2025):
        fetch_file(client,ROOT/f'data/history/security-lists/{year-1}q3.pdf',
                   f'https://www.sec.gov/files/investment/13flist{year-1}q3.pdf')
        print(json.dumps(build(ROOT,year),ensure_ascii=False))
