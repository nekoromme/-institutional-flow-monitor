"""原書類で対応したコードの不足価格を取得する。売買判定は一切しない。"""
from datetime import datetime,timezone
import hashlib
import json
import os
import re
from flow_probe.alpaca import NY,fetch_pages
from flow_probe.bulk13f import digest
from flow_probe.http_client import SafeHttp,ProbeError
from flow_probe.return_diagnostics import validate_price_calendar
from .collect import ROOT,bar_quality,encrypt_bytes,decrypt_bytes,write_json


def requested_symbols(request,year):
    """正式対象の認定とは別に、原書類の対応を通った追加取得候補だけを使う。"""
    values=[]
    for row in request['years'][str(year)]['additional_candidates']:
        if not row.get('cusip_issuer_verified') or not row.get('price_collection_candidate'):
            raise ValueError('price_request_has_unverified_identity')
        ticker=row['ticker_in_reviewed_filing']
        if not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,11}',ticker):
            raise ValueError('price_request_ticker_format_requires_review')
        values.append(ticker)
    if len(values)!=len(set(values)):raise ValueError('multiple_securities_share_ticker_requires_review')
    return tuple(sorted(values))


def main():
    path=ROOT/'research/history/price-request.json'
    request=json.loads(path.read_text())
    secret=os.environ.get('ALPACA_SECRET_KEY','');key=os.environ.get('ALPACA_API_KEY','')
    if not key or len(secret)<16:raise ValueError('market_credentials_missing')
    client=SafeHttp(key,secret,timeout=30,max_requests=180,max_seconds=600)
    report={'code_commit':os.environ.get('GITHUB_SHA','local'),'request_sha256':digest(path),
            'returns_computed':False,'historical_universe_ready':False,'chunks':[],'errors':[]}
    for year in (2023,2024,2025):
        symbols=requested_symbols(request,year)
        if not symbols:continue
        start=f'{year-1}-09-01';end=f'{year+1}-01-31';name=f'additional-{year}'
        try:
            sessions=client.json('https://paper-api.alpaca.markets/v2/calendar',{'start':start,'end':end})
            days=validate_price_calendar(sessions,start=start,end=end)
            prices={};quality={};probes=[]
            for adjustment in ('raw','split','split,dividend'):
                rows,meta=fetch_pages(client,'bars',symbols,
                    datetime.fromisoformat(start+'T00:00:00').replace(tzinfo=NY),
                    datetime.fromisoformat(end+'T23:59:59').replace(tzinfo=NY),
                    feed='sip',timeframe='1Day',adjustment=adjustment,limit=10000,max_pages=12)
                if not meta['complete']:raise ProbeError('historical_market_download_incomplete')
                prices[adjustment]=rows;quality[adjustment]=bar_quality(rows,days);probes.append(meta)
            different=[s for s in symbols if any(
                {r['t'] for r in prices[a][s]}!={r['t'] for r in prices['raw'][s]} for a in prices)]
            payload={'chunk':name,'year':year,'start':start,'end':end,'symbols':symbols,'sessions':sessions,
                'prices':prices,'probes':probes,'request_sha256':digest(path),
                'scope':'historical_identity_linked_price_collection_not_backtest_admission',
                'retrieved_at_utc':datetime.now(timezone.utc).isoformat()}
            raw=json.dumps(payload,sort_keys=True,allow_nan=False).encode()
            encrypted=encrypt_bytes(raw,secret)
            if decrypt_bytes(encrypted,secret)!=raw:raise ValueError('encrypted_roundtrip_failed')
            dest=ROOT/f'data/history/encrypted/{name}.enc';dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(encrypted)
            chunk={'year':year,'chunk':name,'symbols':list(symbols),'start':start,'end':end,'sessions':len(days),
                'quality':quality,'probes':probes,'adjustment_date_mismatch_symbols':different,
                'plaintext_sha256':hashlib.sha256(raw).hexdigest(),'encrypted_sha256':digest(dest),
                'decrypt_roundtrip_verified':True,'backtest_admitted':False}
            report['chunks'].append(chunk)
            write_json(ROOT/f'diagnostics/history/supplement/{name}.json',chunk)
            print(json.dumps({'year':year,'symbols':len(symbols),'records_per_adjustment':sum(q['records'] for q in quality['raw'].values()),
                'missing_symbol_days':sum(q['missing_sessions'] for q in quality['raw'].values())}),flush=True)
        except (ProbeError,ValueError,KeyError,TypeError) as exc:
            report['errors'].append({'year':year,'error':exc.summary() if isinstance(exc,ProbeError) else type(exc).__name__})
    report['http']=client.metrics();report['collection_complete']=not report['errors']
    write_json(ROOT/'diagnostics/history/supplement/summary.json',report)
    print(json.dumps({'collection_complete':report['collection_complete'],'errors':report['errors'],'http':report['http']}))
    return 0 if report['collection_complete'] else 1

if __name__=='__main__':raise SystemExit(main())
