"""長期保有で必要になった翌年の補助価格だけを追加取得する。"""
import json
import os
from datetime import datetime
from flow_probe.alpaca import fetch_pages, NY
from flow_probe.http_client import SafeHttp
from flow_probe.bulk13f import digest
from .collect import ROOT,encrypt_bytes,decrypt_bytes,write_json


def run(root,secret,key):
    client=SafeHttp(key,secret,max_requests=10,max_seconds=120)
    rows,meta=fetch_pages(client,'bars',('HNI','WTM'),
        datetime(2024,1,15,tzinfo=NY),datetime(2024,3,15,23,59,59,tzinfo=NY),
        feed='sip',timeframe='1Day',adjustment='split',limit=10000,max_pages=3)
    if not meta['complete'] or meta['status']!='ok':raise ValueError('extension_incomplete')
    raw=json.dumps({'prices':rows,'metadata':meta},sort_keys=True,allow_nan=False).encode()
    enc=encrypt_bytes(raw,secret);assert decrypt_bytes(enc,secret)==raw
    path=root/'data/history/encrypted/exit-extension.enc';path.write_bytes(enc)
    write_json(root/'diagnostics/history/exit-search/extension.json',
        {'code_commit':os.environ.get('GITHUB_SHA'),'plan_sha256':digest(root/'research/history/EXIT_EXTENSION_PLAN.md'),
         'metadata':meta,'encrypted_sha256':digest(path),'reserved_2025_opened':False})


if __name__=='__main__':run(ROOT,os.environ.get('ALPACA_SECRET_KEY',''),os.environ.get('ALPACA_API_KEY',''))
