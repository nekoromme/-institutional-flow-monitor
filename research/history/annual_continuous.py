"""年初に資金を戻さず、前年の保有をそのまま引き継いで年間損益を示す。"""
import json
import math
import os
from statistics import mean
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
from .annual_portfolio import simulate

SOURCE_HASH = '21a9480a3175607325a581fd82bb3c73d0a0f8b784f80a4368382944eccce48b'


def merge_books(destination, incoming):
    """重複する日の単位や値が違うのに、都合よく上書きしない。"""
    checked = 0
    for symbol, rows in incoming.items():
        dest = destination.setdefault(symbol, {})
        for d, b in rows.items():
            if d in dest:
                old = dest[d]
                if old['valid'] != b['valid'] or any(not math.isclose(
                    old['split'][k], b['split'][k], rel_tol=1e-8, abs_tol=1e-8)
                    for k in ('o','h','l','c','v')):
                    raise ValueError('overlapping_price_basis_mismatch')
                checked += 1
            else:
                dest[d] = b
    return checked


def annual_summaries(record):
    """前年末の保有評価額も含めて翌年の開始資金にする。"""
    result = []
    prior_equity, prior_cash, prior_positions = 1.0, 1.0, 0
    for year in (2023, 2024):
        rows = [r for r in record['daily'] if r['date'].startswith(str(year)+'-')]
        if not rows:
            continue
        fills = [r for r in record['fills'] if r['date'].startswith(str(year)+'-')]
        peak, worst = prior_equity, 0.0
        for r in rows:
            peak = max(peak, r['equity'])
            worst = min(worst, r['equity']/peak-1)
        last = rows[-1]
        result.append({'year':year, 'annual_return':last['equity']/prior_equity-1,
            'start_equity':prior_equity, 'end_equity':last['equity'],
            'profit_as_fraction_of_initial_capital':last['equity']-prior_equity,
            'max_close_drawdown_within_year':worst,
            'average_close_invested_fraction':mean(r['invested_fraction'] for r in rows),
            'incoming_positions':prior_positions, 'incoming_cash':prior_cash,
            'purchases':sum(r['event']=='buy' for r in fills),
            'sales':sum(r['event']=='sell' for r in fills),
            'year_end_open_positions':last['positions'], 'year_end_cash':last['cash'],
            'year_end_stock_value':last['stock_value'], 'sessions':len(rows)})
        prior_equity, prior_cash, prior_positions = last['equity'], last['cash'], last['positions']
    return result


def run(root, secret):
    source = root/'data/history/prior-annual/annual-portfolio.enc'
    if digest(source) != SOURCE_HASH:
        raise ValueError('initial_annual_input_changed')
    captures = json.loads(decrypt_bytes(source.read_bytes(), secret))['benchmark_captures']
    combined, all_scored, calendar, overlap = {}, [], set(), 0
    for year in (2023, 2024):
        payload = load_expanded(root, year, secret)
        books, _ = prepare(payload)
        scored, days = prepare_signals(payload, books, [], year)
        quiet_signals(scored, books, days)
        add_flags(scored, books, days)
        enrich(scored, books, days)
        index, cache = {d:i for i,d in enumerate(days)}, {}
        for r in scored:
            r['cohort_tailwind'] = None
            if r['falling_control'] is None:
                continue
            d = r['date']
            if d not in cache:
                cache[d] = prior_returns(books, days, index[d])
            v = peer_median(cache[d], r['symbol'])
            if v is not None:
                r['cohort_tailwind'] = r['falling_signal'] and v > 0
        overlap += merge_books(combined, books)
        all_scored.extend(scored)
        calendar.update(days)
    keys = [(r['symbol'],r['date']) for r in all_scored]
    if len(keys) != len(set(keys)):
        raise ValueError('duplicate_annual_signals')
    days = sorted(calendar)
    window = [d for d in days if '2023-01-01' <= d <= '2024-12-31']
    for symbol in ('SPY','IWM'):
        combined[symbol] = {d:{'valid':valid_bar(b), 'split':b}
                           for d,b in by_date(captures['split'][symbol]).items()}
    out = {'code_commit':os.environ.get('GITHUB_SHA'), 'reserved_2025_opened':False,
        'plan_sha256':digest(root/'research/history/ANNUAL_CONTINUOUS_PLAN.md'),
        'source_encrypted_sha256':SOURCE_HASH, 'annual_reset_to_cash':False,
        'ticket_fraction_of_initial_capital':.1, 'max_positions':10, 'cost_each_side':.0025,
        'new_market_requests':0, 'overlapping_rows_checked':overlap,
        'dividends_in_primary':False, 'tax_fx_cash_interest_included':False,
        'new_signal_variants':0, 'models':{}}
    private, schedules = {}, {}
    for model in ('falling_signal','cohort_tailwind'):
        attempts, _ = schedule(all_scored, days, model, 10,
                              start='2023-01-01', end='2024-12-31')
        schedules[model] = attempts
        summary, record = simulate(attempts, combined, window)
        private[model] = record
        out['models'][model] = {'summary':summary, 'years':annual_summaries(record)}
    summary, record = simulate(schedules['cohort_tailwind'], combined, window, substitute='SPY')
    private['same_timing_SPY'] = record
    out['models']['same_timing_SPY'] = {'summary':summary, 'years':annual_summaries(record)}
    for symbol in ('SPY','IWM'):
        attempt = [{'symbol':symbol,'entry_date':window[0], 'exit_date':'9999-12-31'}]
        for adjustment in ('split','split,dividend'):
            b = {symbol:{d:{'valid':valid_bar(r), 'split':r}
                         for d,r in by_date(captures[adjustment][symbol]).items()}}
            summary, record = simulate(attempt, b, window, ticket=1, limit=1)
            name = symbol + ('_buy_hold' if adjustment=='split' else '_dividend_reference')
            private[name] = record
            out['models'][name] = {'summary':summary, 'years':annual_summaries(record)}
    for m, result in out['models'].items():
        s = result['summary']
        # simulateのannual_returnは、連続2年を渡すと2年通算。曖昧な名前を変える。
        s['two_year_total_return'] = s.pop('annual_return')
        s['two_year_geometric_annual_return'] = s['end_equity']**.5-1
    raw = json.dumps(private, sort_keys=True, allow_nan=False).encode()
    enc = encrypt_bytes(raw, secret)
    assert decrypt_bytes(enc, secret)==raw
    target=root/'data/history/encrypted/annual-continuous.enc'
    target.write_bytes(enc)
    out['encrypted_sha256']=digest(target)
    write_json(root/'diagnostics/history/annual-continuous/results.json',out)


if __name__=='__main__':
    run(ROOT,os.environ.get('ALPACA_SECRET_KEY',''))
