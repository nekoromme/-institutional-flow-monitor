"""欠損・ゼロ出来高の原因を点検する。売買成績の計算は行わない。

保存済みの2023・2024年データだけを開く。新しい照会は原入力を上書きせず、
別ファイルへ暗号化して残す。少数の標本を全件の証明とは扱わない。
"""
from collections import Counter
from datetime import datetime
import hashlib
import json
import os
from flow_probe.alpaca import fetch_pages,NY
from flow_probe.bulk13f import digest
from flow_probe.http_client import SafeHttp
from flow_probe.returns_core import by_date
from .collect import ROOT,decrypt_bytes,encrypt_bytes,write_json


def load_expanded(root,year,secret):
    if year not in (2023,2024):raise ValueError('reserved_year_must_not_be_opened')
    manifest=json.loads((root/'research/history/evidence/expansion-collection-result.json').read_text())
    expected=next(c for c in manifest['market_chunks'] if c['year']==year)
    path=root/f'data/history/encrypted/expanded-{year}.enc'
    if digest(path)!=expected['encrypted_sha256']:raise ValueError('expanded_ciphertext_changed')
    raw=decrypt_bytes(path.read_bytes(),secret)
    if hashlib.sha256(raw).hexdigest()!=expected['plaintext_sha256']:raise ValueError('expanded_plaintext_changed')
    payload=json.loads(raw)
    if payload['year']!=year:raise ValueError('expanded_year_mismatch')
    return payload


def zero_samples(payload):
    """銘柄・年ごとの最初と最後を選ぶ。好都合な日付を手で選ばない。"""
    output=[]
    for symbol,bars in sorted(payload['prices']['raw'].items()):
        zeros=[(d,b) for d,b in sorted(by_date(bars).items()) if b['v']==0]
        if not zeros:continue
        selected=sorted({zeros[0][0],zeros[-1][0]})
        output.append({'symbol':symbol,'year':payload['year'],'zero_days':[d for d,_ in zeros],
                       'sample_days':selected,'zero_days_with_positive_trade_count':sum(b.get('n',0)>0 for _,b in zeros)})
    return output


def rename_merge(old,replacement,effective_date):
    """切替日より前は旧コード、以後は新コード。既存データを黙って上書きしない。"""
    a,b=by_date(old),by_date(replacement)
    if any(d>=effective_date for d in a):raise ValueError('old_symbol_continues_after_boundary_requires_review')
    merged=[dict(row,source_symbol='LAZY') for d,row in sorted(a.items())]
    merged += [dict(row,source_symbol='GORV') for d,row in sorted(b.items()) if d>=effective_date]
    return sorted(merged,key=lambda row:row['t'])


def main():
    secret=os.environ.get('ALPACA_SECRET_KEY','');key=os.environ.get('ALPACA_API_KEY','')
    client=SafeHttp(key,secret,timeout=25,max_requests=100,max_seconds=480)
    report={'returns_computed':False,'reserved_2025_dataset_opened':False,'code_commit':os.environ.get('GITHUB_SHA'),
            'samples':[],'zero_groups':[],'rename_repair':[],'errors':[]}
    private={'samples':[],'rename_prices':{}}
    payloads={y:load_expanded(ROOT,y,secret) for y in (2023,2024)}
    for year,payload in payloads.items():
        groups=zero_samples(payload);report['zero_groups']+=groups
        for g in groups:
            for day in g['sample_days']:
                start=datetime.fromisoformat(day+'T00:00:00').replace(tzinfo=NY)
                end=datetime.fromisoformat(day+'T23:59:59').replace(tzinfo=NY)
                captures={};metadata={}
                for kind,tf,limit in [('bars','1Day',1000),('bars','1Min',10000),('trades',None,1000)]:
                    label=tf or kind
                    rows,meta=fetch_pages(client,kind,(g['symbol'],),start,end,feed='sip',timeframe=tf,limit=limit,max_pages=1)
                    captures[label]=rows[g['symbol']];metadata[label]=meta
                daily=captures['1Day'];minutes=captures['1Min'];trades=captures['trades']
                row={'year':year,'symbol':g['symbol'],'date':day,'saved_daily_volume_was_zero':True,
                     'refetched_daily_rows':len(daily),'refetched_daily_has_positive_volume':any(b['v']>0 for b in daily),
                     'minute_rows':len(minutes),'positive_volume_minute_rows':sum(b['v']>0 for b in minutes),
                     'trade_rows_in_sample':len(trades),'trade_conditions':dict(Counter(','.join(t.get('c',[])) for t in trades)),
                     'metadata':metadata,'zero_volume_certified_as_no_trading':False}
                report['samples'].append(row);private['samples'].append({'year':year,'symbol':g['symbol'],'date':day,'data':captures,'metadata':metadata})
                print(json.dumps({k:v for k,v in row.items() if k not in ('metadata','trade_conditions')}),flush=True)
    # 会社発表で確認したコード変更の後だけを取得する。2025年専用ファイルは開かない。
    start=datetime(2024,1,17,tzinfo=NY);end=datetime(2025,1,31,23,59,59,tzinfo=NY)
    for adjustment in ('raw','split','split,dividend'):
        rows,meta=fetch_pages(client,'bars',('GORV',),start,end,feed='sip',timeframe='1Day',adjustment=adjustment,limit=10000,max_pages=2)
        private['rename_prices'][adjustment]={'bars':rows['GORV'],'metadata':meta}
        if not meta['complete']:report['errors'].append({'kind':'incomplete_rename_prices','adjustment':adjustment})
    if not report['errors']:
        repaired={}
        for year,payload in payloads.items():
            days={s['date'] for s in payload['sessions']};repaired[year]={}
            for adjustment in ('raw','split','split,dividend'):
                replacement=[b for b in private['rename_prices'][adjustment]['bars'] if datetime.fromisoformat(b['t'].replace('Z','+00:00')).astimezone(NY).date().isoformat() in days]
                old=payload['prices'][adjustment]['LAZY'];merged=rename_merge(old,replacement,'2024-01-17');repaired[year][adjustment]=merged
                observed=set(by_date(merged));report['rename_repair'].append({'year':year,'adjustment':adjustment,'old_rows':len(old),'repaired_rows':len(merged),'missing_dates':sorted(days-observed),'zero_volume_rows':sum(b['v']==0 for b in merged),'adjustment_units_certified':False})
        private['repaired_LAZY']=repaired
    raw=json.dumps(private,sort_keys=True,allow_nan=False).encode();cipher=encrypt_bytes(raw,secret)
    if decrypt_bytes(cipher,secret)!=raw:raise ValueError('gap_audit_roundtrip_failed')
    dest=ROOT/'data/history/encrypted/gap-audit.enc';dest.write_bytes(cipher)
    report.update(encrypted_sha256=digest(dest),plaintext_sha256=hashlib.sha256(raw).hexdigest(),http=client.metrics())
    write_json(ROOT/'diagnostics/history/gap-audit/summary.json',report)
    return 0 if not report['errors'] else 1

if __name__=='__main__':raise SystemExit(main())
