"""取得口を集約する。非公開の価格を標準出力へ出さない。

Alpacaの配信区分・補正方式を固定し、権限がない時に別配信へ逃がさない。
SECとFREDも読み取りだけ。売買APIは使わない。
"""
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, build_opener
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo

from flow_probe.alpaca import fetch_pages
from flow_probe.http_client import SafeHttp, ProbeError, NoRedirect
from .store import utcnow, timestamp, validate_bars

NY = ZoneInfo('America/New_York')


def collect_prices(store, symbols, start, end, *, client=None):
    """日足をsplit補正で取得。配当込み収益と混同しない。

    日足は通常時間限定とは主張しない。プレ・寄り検証には既存の分足を使う。
    当日の未確定足を混ぜないよう、NYの今日より前だけを受け付ける。
    """
    if not symbols or len(symbols) > 50 or any(not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}', s) for s in symbols):
        raise ValueError('invalid_symbols')
    begin = datetime.fromisoformat(start).replace(tzinfo=NY)
    finish = datetime.fromisoformat(end).replace(tzinfo=NY)
    if finish.date() >= datetime.now(NY).date() or begin > finish:
        raise ValueError('require_completed_date_range')
    if (finish - begin).days > 3653:
        raise ValueError('maximum_ten_years_per_request')
    key = os.environ.get('ALPACA_API_KEY', '')
    secret = os.environ.get('ALPACA_SECRET_KEY', '')
    if client is None:
        if not key or not secret:
            raise ValueError('alpaca_credentials_missing')
        client = SafeHttp(key, secret, max_requests=200, max_seconds=540)
    calendar = client.json('https://paper-api.alpaca.markets/v2/calendar', {'start': start, 'end': end})
    if not isinstance(calendar, list) or any(not isinstance(x, dict) or 'date' not in x for x in calendar):
        raise ValueError('unexpected_calendar_schema')
    sessions = sorted(x['date'] for x in calendar)
    if not sessions or len(set(sessions)) != len(sessions) or any(not start <= x <= end for x in sessions):
        raise ValueError('invalid_calendar')
    prices, meta = fetch_pages(client, 'bars', tuple(symbols), begin,
                              finish + timedelta(days=1) - timedelta(microseconds=1),
                              feed='sip', timeframe='1Day', adjustment='split', max_pages=100)
    if not meta['complete']:
        raise ValueError('market_download_incomplete')
    output = []
    for symbol in symbols:
        rows = [{'date': timestamp(r['t']).astimezone(NY).date().isoformat(),
                 **{long: r[short] for long, short in [('open','o'),('high','h'),('low','l'),('close','c'),('volume','v')]}}
                for r in prices[symbol]]
        quality = validate_bars(rows, sessions)
        metadata = {'retrieved_at_utc': utcnow(), 'start': start, 'end': end,
                    'currency': 'USD', 'exchange_timezone': 'America/New_York',
                    'feed': 'sip', 'adjustment': 'split', 'timeframe': '1Day',
                    'session_scope': 'provider_daily_not_rth_only', 'sessions': sessions,
                    'calendar_source': 'Alpaca US calendar', 'quality': quality,
                    'point_in_time_adjustments': False, 'cash_dividends_included': False,
                    'source_url': 'https://data.alpaca.markets/v2/stocks/bars', 'download': meta}
        dataset_id = store.put('alpaca', symbol, metadata, prices[symbol], rows)
        output.append({'symbol': symbol, 'dataset_id': dataset_id, **quality})
    return output


def fred_json(params, opener=None):
    """APIキー付きURLや応答本文をエラーに載せない。別ホストへの転送も拒否。"""
    opener = opener or build_opener(NoRedirect())
    for attempt in range(3):
        try:
            req = Request('https://api.stlouisfed.org/fred/series/observations?' + urlencode(params),
                          headers={'User-Agent': 'market-hub/0.1'}, method='GET')
            with opener.open(req, timeout=20) as response:
                body = response.read(16_000_001)
                if len(body) > 16_000_000:
                    raise ValueError('fred_response_too_large')
                return json.loads(body)
        except HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                retry = exc.headers.get('Retry-After', '') if exc.headers else ''
                if retry.isdigit() and int(retry) > 20:
                    raise ValueError('fred_rate_limit_budget') from None
                time.sleep(int(retry) if retry.isdigit() else 2 ** attempt)
                continue
            raise ValueError('fred_http_failure') from None
        except (URLError, TimeoutError):
            if attempt == 2:
                raise ValueError('fred_network_failure') from None
            time.sleep(2 ** attempt)
    raise ValueError('fred_retry_exhausted')


def collect_macro(store, series, start, end, vintage, *, fetch=fred_json):
    """経済指標は『当時の版』を明示。最新改定値を過去予測に使わない。"""
    for day in (start, end, vintage):
        datetime.fromisoformat(day)
    if start > end or end > vintage or vintage > datetime.now(timezone.utc).date().isoformat():
        raise ValueError('invalid_macro_dates')
    if not re.fullmatch(r'[A-Za-z0-9_]{1,80}', series):
        raise ValueError('invalid_series')
    key = os.environ.get('FRED_API_KEY', '')
    if not key:
        raise ValueError('fred_credentials_missing')
    params = dict(api_key=key, file_type='json', series_id=series, observation_start=start,
                  observation_end=end, realtime_start=vintage, realtime_end=vintage, limit=10000, offset=0)
    rows = []
    while True:
        payload = fetch(params)
        if not isinstance(payload.get('observations'), list):
            raise ValueError('unexpected_fred_schema')
        rows.extend(payload['observations'])
        params['offset'] += len(payload['observations'])
        if params['offset'] >= int(payload['count']):
            break
        if not payload['observations'] or params['offset'] > 100000:
            raise ValueError('fred_incomplete')
    metadata = dict(retrieved_at_utc=utcnow(), start=start, end=end, vintage=vintage,
                    source_url='https://api.stlouisfed.org/fred/series/observations',
                    interpretation='as_of_vintage_date; not_intraday_release_time; dot_is_missing')
    return store.put('fred', series, metadata, rows)


def collect_sec(store, cik, *, client=None):
    """提出履歴とXBRLを取得。会計期末と公表日を分離した原データを保存。"""
    if not re.fullmatch(r'\d{1,10}', cik):
        raise ValueError('invalid_cik')
    cik = cik.zfill(10)
    client = client or SafeHttp()
    output = {}
    for kind, route in [('submissions', f'submissions/CIK{cik}.json'),
                        ('companyfacts', f'api/xbrl/companyfacts/CIK{cik}.json')]:
        url = 'https://data.sec.gov/' + route
        payload = client.json(url)
        if not isinstance(payload, dict) or str(payload.get('cik', '')).zfill(10) != cik:
            raise ValueError('unexpected_sec_entity')
        output[kind] = store.put('sec_' + kind, cik,
            {'retrieved_at_utc': utcnow(), 'source_url': url,
             'scope': 'recent_plus_archive_references' if kind == 'submissions' else 'standard_entity_facts',
             'point_in_time_rule': 'filter filed/acceptanceDateTime before decision; no latest-value backfill'}, payload)
    return output
