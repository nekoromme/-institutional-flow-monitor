"""同じ監視対象の値動きと比較する。市場全体の指数とは扱わない。"""
import json
import os
from statistics import median, mean
from flow_probe.bulk13f import digest
from flow_probe.returns_core import schedule, evaluate, summarize, price_path, after_cost
from .collect import ROOT, write_json, encrypt_bytes, decrypt_bytes
from .gap_audit import load_expanded
from .practical_validation import prepare
from .development_validation import prepare_signals
from .quiet_trial import quiet_signals
from .downside_trial import add_flags
from .reversal_trial import enrich, compare_pass


def peer_median(returns, excluded, minimum=30):
    """自分自身を比較相手に混ぜない。不足時はゼロ扱いしない。"""
    values = [r for symbol, r in returns.items() if symbol != excluded]
    return median(values) if len(values) >= minimum else None


def prior_returns(books, days, i):
    """通知日までの20営業日騰落。窓内の全日が有効な銘柄だけ使う。"""
    if i < 20:
        return {}
    window = days[i-20:i+1]
    return {s: b[window[-1]]['split']['c']/b[window[0]]['split']['c']-1
            for s, b in books.items() if all(d in b and b[d]['valid'] for d in window)}


def run(root, secret):
    out = {'code_commit': os.environ.get('GITHUB_SHA'),
           'plan_sha256': digest(root/'research/history/COHORT_CONTEXT_PLAN.md'),
           'reserved_2025_opened': False, 'prior_strategy_variants': 14,
           'new_strategy_variants': 1, 'benchmark_is_market_index': False, 'years': []}
    private = []
    for year in (2023, 2024):
        p = load_expanded(root, year, secret)
        books, _ = prepare(p)
        scored, days = prepare_signals(p, books, [], year)
        quiet_signals(scored, books, days)
        add_flags(scored, books, days)
        enrich(scored, books, days)
        index = {d: i for i, d in enumerate(days)}
        cache = {}
        for r in scored:
            r.update(cohort_tailwind=None, tailwind_control=None)
            if r['falling_control'] is None:
                continue
            d = r['date']
            if d not in cache:
                cache[d] = prior_returns(books, days, index[d])
            value = peer_median(cache[d], r['symbol'])
            r['prior_peer_median'] = value
            if value is not None:
                r['cohort_tailwind'] = r['falling_signal'] and value > 0
                r['tailwind_control'] = r['falling_control'] and value > 0
        ledgers, summaries, halves, skips = {}, {}, {}, {}
        for model in ('falling_signal', 'cohort_tailwind', 'tailwind_control'):
            a, skip = schedule(scored, days, model, 10,
                start=f'{year}-01-01', end=f'{year}-12-31')
            rows = evaluate(a, books, days)
            ledgers[model], summaries[model], skips[model] = rows, summarize(rows), skip
            halves[model] = {h: summarize([t for t in rows if (t['signal_date'][5:7] <= '06') == first])
                for h, first in [('first', True), ('second', False)]}
        # ここからの同時期の他銘柄リターンは事後診断。購入可否には戻さない。
        pairs = []
        for t in ledgers['falling_signal']:
            if t['status'] != 'priced':
                continue
            i, j = index[t['entry_date']], index[t['exit_date']]
            returns = {}
            for s, b in books.items():
                path = price_path(b, days, i, j)
                if path is not None:
                    returns[s] = path['ratio'] - 1
            peer = peer_median(returns, t['symbol'])
            prior = peer_median(cache[t['signal_date']], t['symbol'])
            pairs.append({**t, 'peer_return': peer, 'prior_peer_median': prior,
                'net_minus_peer_net': t['net_return'] - after_cost(1+peer, .0025) if peer is not None else None})
        def describe(rows):
            known = [t for t in rows if t['peer_return'] is not None]
            return {'count': len(rows), 'peer_pairs': len(known),
                'mean_net': mean(t['net_return'] for t in rows) if rows else None,
                'mean_peer_price_return': mean(t['peer_return'] for t in known) if known else None,
                'mean_net_minus_peer_net': mean(t['net_minus_peer_net'] for t in known) if known else None}
        context = {'all': describe(pairs),
            'halves': {h: describe([t for t in pairs if (t['signal_date'][5:7] <= '06') == first])
                       for h, first in [('first', True), ('second', False)]},
            'prior_up': {str(up): describe([t for t in pairs if t['prior_peer_median'] is not None and (t['prior_peer_median'] > 0) == up]) for up in (True, False)}}
        out['years'].append({'year': year, 'summaries': summaries, 'halves': halves,
                            'parent_peer_context': context, 'skips': skips})
        private.append({'year': year, 'ledgers': ledgers, 'peer_pairs': pairs})
    out['passes'] = all(compare_pass(y['summaries']['cohort_tailwind'],
        y['summaries']['falling_signal'], y['summaries']['tailwind_control']) for y in out['years'])
    raw = json.dumps(private, sort_keys=True, allow_nan=False).encode()
    enc = encrypt_bytes(raw, secret)
    assert decrypt_bytes(enc, secret) == raw
    path = root/'data/history/encrypted/cohort-context-ledgers.enc'
    path.write_bytes(enc)
    out['encrypted_sha256'] = digest(path)
    write_json(root/'diagnostics/history/cohort-context/results.json', out)


if __name__ == '__main__':
    run(ROOT, os.environ.get('ALPACA_SECRET_KEY', ''))
