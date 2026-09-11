"""取引平均を年利と取り違えないため、現金を含む口座を日々計算する。

金額は年初資金を1とした比率。円に換算する場合は100万円等を掛ける。
米ドル基準の計算なので、円での実際の損益には別途為替が加わる。
"""
import json
import os
from collections import defaultdict, Counter
from datetime import datetime
from statistics import mean
from flow_probe.alpaca import fetch_pages, NY
from flow_probe.http_client import SafeHttp
from flow_probe.bulk13f import digest
from flow_probe.returns_core import schedule, by_date, valid_bar
from .collect import ROOT, write_json, encrypt_bytes, decrypt_bytes
from .gap_audit import load_expanded
from .practical_validation import prepare
from .development_validation import prepare_signals
from .quiet_trial import quiet_signals
from .downside_trial import add_flags
from .reversal_trial import enrich
from .cohort_context_trial import prior_returns, peer_median


def simulate(attempts, books, days, *, substitute=None, cost=.0025, ticket=.1, limit=10):
    """寄付きで購入し、引けで予定売却、その後の資産を記録する。

    将来の勝ち負けで採用候補を選ばない。指数代替では複数の候補を
    同じ指数の別口の購入として管理するため、銘柄名ではなく番号で保有する。
    """
    if not days or days != sorted(set(days)):
        raise ValueError('invalid_annual_calendar')
    if not 0 < ticket <= 1 or not 0 <= cost < 1 or limit < 1:
        raise ValueError('invalid_portfolio_settings')
    arrivals = defaultdict(list)
    for i, t in enumerate(attempts):
        if t['entry_date'] in days:
            if not t['exit_date']:
                raise ValueError('unknown_scheduled_exit')
            arrivals[t['entry_date']].append({**t, 'id': i})
    cash, peak, worst, max_held = 1.0, 1.0, 0.0, 0
    positions, fills, daily = {}, [], []
    skipped = Counter()

    def bar(symbol, day):
        b = books.get(symbol, {}).get(day)
        if not b or not b['valid']:
            raise ValueError('missing_portfolio_valuation_price')
        return b['split']

    for d in days:
        # 当日引けの売却代金はまだない。寄付きの現金だけで購入する。
        for t in sorted(arrivals[d], key=lambda r: (r['symbol'], r['id'])):
            if len(positions) >= limit:
                skipped['position_limit'] += 1
                continue
            if cash + 1e-12 < ticket:
                skipped['insufficient_cash'] += 1
                continue
            symbol = substitute or t['symbol']
            price = bar(symbol, d)['o']
            quantity = ticket / ((1 + cost) * price)
            cash -= ticket
            if abs(cash) < 1e-12:
                cash = 0.0
            positions[t['id']] = {**t, 'asset': symbol, 'quantity': quantity,
                'cash_paid': ticket, 'purchase_price': price}
            fills.append({'id': t['id'], 'event': 'buy', 'date': d,
                          'asset': symbol, 'cash': ticket})
        max_held = max(max_held, len(positions))
        for ident, p in list(positions.items()):
            if p['exit_date'] == d:
                received = p['quantity'] * bar(p['asset'], d)['c'] * (1-cost)
                cash += received
                fills.append({'id': ident, 'event': 'sell', 'date': d,
                              'asset': p['asset'], 'cash': received})
                del positions[ident]
        market_value = sum(p['quantity'] * bar(p['asset'], d)['c'] for p in positions.values())
        equity = cash + market_value
        if cash < -1e-10 or equity <= 0:
            raise ValueError('invalid_cash_or_equity')
        peak = max(peak, equity)
        worst = min(worst, equity/peak-1)
        daily.append({'date': d, 'cash': cash, 'stock_value': market_value,
                      'equity': equity, 'positions': len(positions),
                      'invested_fraction': market_value/equity})
    summary = {'annual_return': daily[-1]['equity']-1,
        'end_equity': daily[-1]['equity'], 'max_close_drawdown': worst,
        'average_close_invested_fraction': mean(r['invested_fraction'] for r in daily),
        'max_simultaneous_positions': max_held,
        'purchases': sum(r['event']=='buy' for r in fills),
        'sales': sum(r['event']=='sell' for r in fills),
        'year_end_open_positions': len(positions), 'year_end_cash': cash,
        'year_end_stock_value': daily[-1]['stock_value'], 'skipped': dict(skipped),
        'first_session': days[0], 'last_session': days[-1], 'sessions': len(days)}
    return summary, {'daily': daily, 'fills': fills, 'open_positions': list(positions.values())}


def run(root, secret, key):
    # 指数の参考データだけを新規取得。口座や注文へのアクセスはない。
    client = SafeHttp(key, secret, max_requests=30, max_seconds=180)
    start = datetime(2023, 1, 1, tzinfo=NY)
    end = datetime(2024, 12, 31, 23, 59, 59, tzinfo=NY)
    captures, metadata = {}, {}
    for adjustment in ('split', 'split,dividend'):
        rows, meta = fetch_pages(client, 'bars', ('SPY', 'IWM'), start, end,
            feed='sip', timeframe='1Day', adjustment=adjustment, limit=10000, max_pages=5)
        if not meta['complete'] or meta['status'] != 'ok':
            raise ValueError('benchmark_capture_incomplete')
        captures[adjustment], metadata[adjustment] = rows, meta
    out = {'code_commit': os.environ.get('GITHUB_SHA'),
        'plan_sha256': digest(root/'research/history/ANNUAL_PLAN.md'),
        'reserved_2025_opened': False, 'annual_reset_to_cash': True,
        'ticket_fraction_of_initial_capital': .1, 'max_positions': 10,
        'cost_each_side': .0025, 'dividends_in_primary': False,
        'cash_interest': 0, 'tax_and_fx_included': False,
        'new_signal_variants': 0, 'benchmark_metadata': metadata, 'years': []}
    private = {'benchmark_captures': captures, 'years': []}
    for year in (2023, 2024):
        payload = load_expanded(root, year, secret)
        books, _ = prepare(payload)
        scored, days = prepare_signals(payload, books, [], year)
        quiet_signals(scored, books, days)
        add_flags(scored, books, days)
        enrich(scored, books, days)
        index = {d: i for i, d in enumerate(days)}
        cache = {}
        for r in scored:
            r['cohort_tailwind'] = None
            if r['falling_control'] is None:
                continue
            d = r['date']
            if d not in cache:
                cache[d] = prior_returns(books, days, index[d])
            value = peer_median(cache[d], r['symbol'])
            if value is not None:
                r['cohort_tailwind'] = r['falling_signal'] and value > 0
        annual_days = [d for d in days if d.startswith(str(year)+'-')]
        # 比較用ETFは候補計算後に追加し、周囲の銘柄中央値を変えない。
        for symbol in ('SPY', 'IWM'):
            rows = by_date(captures['split'][symbol])
            books[symbol] = {d: {'valid': valid_bar(b), 'split': b} for d, b in rows.items()}
        summaries, records, schedules = {}, {}, {}
        for model in ('falling_signal', 'cohort_tailwind'):
            attempts, _ = schedule(scored, days, model, 10,
                start=f'{year}-01-01', end=f'{year}-12-31')
            schedules[model] = attempts
            summaries[model], records[model] = simulate(attempts, books, annual_days)
        summaries['same_timing_SPY'], records['same_timing_SPY'] = simulate(
            schedules['cohort_tailwind'], books, annual_days, substitute='SPY')
        for symbol in ('SPY', 'IWM'):
            # 年初から全額を保有し、年末に売却して両側の費用を控除。
            attempt = [{'symbol': symbol, 'signal_date': annual_days[0],
                        'entry_date': annual_days[0], 'exit_date': annual_days[-1]}]
            summaries[symbol+'_buy_hold'], records[symbol+'_buy_hold'] = simulate(
                attempt, books, annual_days, ticket=1, limit=1)
            adjusted = by_date(captures['split,dividend'][symbol])
            reference = {symbol: {d: {'valid': valid_bar(b), 'split': b} for d, b in adjusted.items()}}
            summaries[symbol+'_dividend_reference'], _ = simulate(
                attempt, reference, annual_days, ticket=1, limit=1)
        out['years'].append({'year': year, 'summaries': summaries,
                            'scheduled_counts': {m: len(v) for m, v in schedules.items()}})
        private['years'].append({'year': year, 'records': records, 'schedules': schedules})
    raw = json.dumps(private, sort_keys=True, allow_nan=False).encode()
    enc = encrypt_bytes(raw, secret)
    assert decrypt_bytes(enc, secret) == raw
    target = root/'data/history/encrypted/annual-portfolio.enc'
    target.write_bytes(enc)
    out['encrypted_sha256'] = digest(target)
    write_json(root/'diagnostics/history/annual/results.json', out)


if __name__ == '__main__':
    run(ROOT, os.environ.get('ALPACA_SECRET_KEY', ''), os.environ.get('ALPACA_API_KEY', ''))
