"""固定した読み取り先だけへ接続する。キーや応答本文は例外へ載せない。

既存の株価取得用HTTPとは分ける。オプションの追加によって注文・口座APIへ
アクセスできる範囲を広げないため。
"""
import json
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, build_opener
from flow_probe.http_client import NoRedirect, ProbeError, error_category


class SourceHttp:
    def __init__(self, *, opener=None, timeout=20, max_requests=100, interval=.5):
        self.opener = opener or build_opener(NoRedirect())
        self.timeout = timeout
        self.max_requests = max_requests
        self.interval = interval
        self.calls = 0
        self.previous = 0

    def read(self, url, params=None, *, max_bytes=16_000_000):
        parsed = urlparse(url)
        host, path = parsed.hostname, parsed.path
        allowed = (
            host == 'fred.stlouisfed.org' and path == '/graph/fredgraph.csv' or
            host == 'fchart.stock.naver.com' and path == '/sise.nhn' or
            host == 'data.nasdaq.com' and path == '/api/v3/datatables/NDAQ/RTAT10.json' or
            host == 'data.alpaca.markets' and (
                path in {'/v1beta1/options/bars','/v1beta1/options/trades'} or
                re.fullmatch(r'/v1beta1/options/snapshots/[A-Z][A-Z0-9.\-]{0,14}',path))
        )
        if not allowed or parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None,443) or parsed.query or parsed.fragment:
            raise ProbeError('destination_not_allowed')
        headers = {'User-Agent':'market-hub/0.2 (personal financial research)', 'Accept':'*/*'}
        if host == 'data.alpaca.markets':
            key, secret = os.environ.get('ALPACA_API_KEY',''),os.environ.get('ALPACA_SECRET_KEY','')
            if not key or not secret:
                raise ProbeError('alpaca_credentials_missing')
            headers.update({'APCA-API-KEY-ID':key,'APCA-API-SECRET-KEY':secret})
        full_url = url + ('?' + urlencode(params) if params else '')
        for attempt in range(3):
            if self.calls >= self.max_requests:
                raise ProbeError('source_request_budget_exceeded')
            time.sleep(max(0,self.interval-(time.monotonic()-self.previous)))
            self.previous = time.monotonic()
            self.calls += 1
            try:
                with self.opener.open(Request(full_url,headers=headers,method='GET'),timeout=self.timeout) as response:
                    raw = response.read(max_bytes+1)
                    if len(raw)>max_bytes:
                        raise ProbeError('source_response_too_large')
                    return raw
            except HTTPError as exc:
                body = exc.read(4096)
                if exc.code in (429,500,502,503,504) and attempt<2:
                    retry = exc.headers.get('Retry-After','') if exc.headers else ''
                    if retry.isdigit() and int(retry)>20:
                        raise ProbeError('source_rate_limit_budget',exc.code) from None
                    time.sleep(int(retry) if retry.isdigit() else 2**attempt)
                    continue
                raise ProbeError(error_category(exc.code,body),exc.code) from None
            except (URLError,TimeoutError,ConnectionError):
                if attempt==2:
                    raise ProbeError('source_network_failure') from None
                time.sleep(2**attempt)
        raise ProbeError('source_retry_exhausted')

    def json(self, url, params=None):
        try:
            return json.loads(self.read(url,params))
        except (UnicodeDecodeError,json.JSONDecodeError):
            raise ProbeError('source_invalid_json') from None
