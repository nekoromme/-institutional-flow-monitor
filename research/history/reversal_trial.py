"""下落シグナルの失敗原因と、事前固定した改善仮説を検証する。

将来の株価は原因診断と損益計算だけに使う。購入条件の計算関数には
当日までの価格だけを渡し、勝ち取引を後から選ぶ処理と分けている。
"""
import json
import os
from collections import defaultdict
from statistics import median
from flow_probe.bulk13f import digest
from flow_probe.returns_core import schedule, evaluate, summarize
from .collect import ROOT, write_json, encrypt_bytes, decrypt_bytes
from .gap_audit import load_expanded
from .practical_validation import prepare
from .development_validation import prepare_signals
from .quiet_trial import quiet_signals
from .downside_trial import add_flags
from .failure_diagnostics import describe


def mechanism_flags(bar, prior_closes):
    """当日が前日比下落でも、高安中点まで戻していれば回復ありとする。
    値幅ゼロは方向の材料がないので回復条件には使わない。
    長期基準は当日を除く60日で固定する。
    """
    recovery = bar['h'] > bar['l'] and bar['c'] >= (bar['h'] + bar['l']) / 2
    intact = len(prior_closes) == 60 and bar['c'] >= median(prior_closes)
    return recovery, intact


def enrich(scored, books, days):
    index = {d: i for i, d in enumerate(days)}
    for r in scored:
        r.update(falling_control=None, recovery_close=None, intact_trend=None,
                 recovery_control=None, trend_control=None, volume_fade=None,
                 depressed=None, fade_control=None, depressed_control=None)
        if r['no_up_control'] is None:
            continue
        i = index[r['date']]
        book = books[r['symbol']]
        recovery, intact = mechanism_flags(book[r['date']]['split'],
            [book[d]['split']['c'] for d in days[i-60:i]])
        # 売買量の山を越えたか。価格の下げ止まりは要求しない。
        fading = book[r['date']]['split']['v'] < max(book[d]['split']['v'] for d in days[i-4:i])
        control = r['no_up_control'] and r['signal_day_down']
        r.update(falling_control=control, recovery_close=r['falling_signal'] and recovery,
                 intact_trend=r['falling_signal'] and intact,
                 recovery_control=control and recovery, trend_control=control and intact,
                 recovery_flag=recovery, intact_flag=intact,
                 volume_fade=r['falling_signal'] and fading,
                 depressed=r['falling_signal'] and not intact,
                 fade_control=control and fading, depressed_control=control and not intact)


def compare_pass(s, parent, control):
    return (s['priced'] >= 30 and s['mean_net_return'] > 0
            and s['without_best_trade_mean'] > 0
            and s['mean_net_return'] > parent['mean_net_return']
            and s['mean_net_return'] > control['mean_net_return'])


def run(root, secret):
    out = {'code_commit': os.environ.get('GITHUB_SHA'),
           'plan_sha256': digest(root/'research/history/REVERSAL_PLAN.md'),
           'reserved_2025_opened': False, 'prior_strategy_variants': 10,
           'new_strategy_variants': 2, 'years': [], 'causation_established': False}
    private = []
    for year in (2023, 2024):
        payload = load_expanded(root, year, secret)
        books, _ = prepare(payload)
        scored, days = prepare_signals(payload, books, [], year)
        quiet_signals(scored, books, days)
        add_flags(scored, books, days)
        enrich(scored, books, days)
        flags = {(r['symbol'], r['date']): r for r in scored}
        ledgers, summaries, halves = {}, {}, {}
        models = ['falling_signal', 'falling_control', 'recovery_close', 'intact_trend',
                  'recovery_control', 'trend_control']
        if os.environ.get('REVERSAL_FOLLOWUP') == '1':
            models += ['volume_fade', 'depressed', 'fade_control', 'depressed_control']
        for model in models:
            attempts, _ = schedule(scored, days, model, 10,
                                   start=f'{year}-01-01', end=f'{year}-12-31')
            ledger = evaluate(attempts, books, days)
            ledgers[model] = ledger
            summaries[model] = summarize(ledger)
            halves[model] = {name: summarize([t for t in ledger
                if (t['signal_date'][5:7] <= '06') == first])
                for name, first in [('first', True), ('second', False)]}
        parent = [describe(t, books, days) for t in ledgers['falling_signal']]
        shapes = defaultdict(list)
        for t in parent:
            shapes[t.get('failure_shape', 'unresolved')].append(t)
        slices = {}
        for field in ('recovery_flag', 'intact_flag'):
            slices[field] = {str(keep): summarize([t for t in parent
                if flags[(t['symbol'], t['signal_date'])][field] is keep]) for keep in (True, False)}
        slices['entry_gap'] = {str(up): summarize([t for t in parent
            if t.get('entry_gap') is not None and (t['entry_gap'] > 0) == up]) for up in (True, False)}
        # 同じ購入日で保有日数だけ変える比較。これは独立した売買成績ではない。
        horizons = {}
        for horizon in (5, 10, 20):
            changed = []
            for t in ledgers['falling_signal']:
                j = days.index(t['signal_date']) + horizon
                changed.append({**t, 'holding_sessions': horizon,
                    'exit_date': days[j] if j < len(days) else None})
            horizons[str(horizon)] = summarize(evaluate(changed, books, days))
        out['years'].append({'year': year, 'summaries': summaries, 'halves': halves,
            'failure_shapes': {k: summarize(v) for k, v in shapes.items()},
            'parent_slices': slices, 'fixed_entry_horizons': horizons})
        private.append({'year': year, 'scored': scored, 'ledgers': ledgers, 'parent_diagnosed': parent})
    # 両年で短期の方がよいという事前に定めた条件だけで次の検証へ進む。
    out['shorter_executed'] = all(y['fixed_entry_horizons']['5']['mean_net_return'] >
        y['fixed_entry_horizons']['10']['mean_net_return'] for y in out['years'])
    if out['shorter_executed']:
        out['new_strategy_variants'] += 1
        for y, item in zip(out['years'], private):
            year = y['year']
            payload = load_expanded(root, year, secret)
            books, _ = prepare(payload)
            days = [s['date'] for s in payload['sessions']]
            for field, label in [('falling_signal', 'falling_short5'), ('falling_control', 'control_short5')]:
                attempts, _ = schedule(item['scored'], days, field, 5,
                    start=f'{year}-01-01', end=f'{year}-12-31')
                ledger = evaluate(attempts, books, days)
                item['ledgers'][label] = ledger
                y['summaries'][label] = summarize(ledger)
                y['halves'][label] = {name: summarize([t for t in ledger
                    if (t['signal_date'][5:7] <= '06') == first])
                    for name, first in [('first', True), ('second', False)]}
    controls = {'recovery_close': 'recovery_control', 'intact_trend': 'trend_control'}
    if out['shorter_executed']:
        controls['falling_short5'] = 'control_short5'
    if os.environ.get('REVERSAL_FOLLOWUP') == '1':
        out['followup_plan_sha256'] = digest(root/'research/history/REVERSAL_FOLLOWUP.md')
        out['new_strategy_variants'] += 2
        controls.update(volume_fade='fade_control', depressed='depressed_control')
    out['passing_models'] = [m for m, c in controls.items() if all(
        compare_pass(y['summaries'][m], y['summaries']['falling_signal'], y['summaries'][c])
        for y in out['years'])]
    out['comparison_controls'] = controls
    raw = json.dumps(private, sort_keys=True, allow_nan=False).encode()
    enc = encrypt_bytes(raw, secret)
    assert decrypt_bytes(enc, secret) == raw
    path = root/'data/history/encrypted/reversal-ledgers.enc'
    path.write_bytes(enc)
    out['encrypted_sha256'] = digest(path)
    write_json(root/'diagnostics/history/reversal/results.json', out)
    print(json.dumps({'passing_models': out['passing_models'], 'shorter_executed': out['shorter_executed']}))


if __name__ == '__main__':
    run(ROOT, os.environ.get('ALPACA_SECRET_KEY', ''))
