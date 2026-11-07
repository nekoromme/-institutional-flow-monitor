"""条件を固定して検証する。先の値段を見てからシグナルを選ばない。

翌取引日の始値で入り、h取引日目の終値で出る。日足での仮想約定であり、
実際にその値段で売買できた保証ではない。レバETFはETFそのものの価格を使う。
"""
import math
import statistics
from .store import fingerprint


def rsi(closes, period=14):
    """Wilder方式。初回は単純平均、その後は平滑化。初期不足はNone。"""
    if len(closes) <= period:
        return None
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gain = sum(max(0, x) for x in changes[:period]) / period
    loss = sum(max(0, -x) for x in changes[:period]) / period
    for change in changes[period:]:
        gain = (gain * (period - 1) + max(0, change)) / period
        loss = (loss * (period - 1) + max(0, -change)) / period
    if gain == loss == 0:
        return 50.0
    return 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)


def indicators(rows):
    if not rows:
        raise ValueError('empty_prices')
    closes = [x['close'] for x in rows]
    last = closes[-1]
    output = {'as_of': rows[-1]['date'], 'rsi14': rsi(closes), 'close': last}
    for n in (20, 50, 200, 250):
        output[f'sma{n}'] = statistics.mean(closes[-n:]) if len(closes) >= n else None
        output[f'distance_sma{n}'] = last / output[f'sma{n}'] - 1 if output[f'sma{n}'] else None
    output['return_5_sessions'] = last / closes[-6] - 1 if len(closes) > 5 else None
    output['return_20_sessions'] = last / closes[-21] - 1 if len(closes) > 20 else None
    prior = [x['volume'] for x in rows[-61:-1]]
    output['volume_vs_prior60_median'] = rows[-1]['volume'] / statistics.median(prior) if len(prior) == 60 else None
    output['drawdown_from_observed_peak'] = last / max(closes) - 1
    return output


def signals(rows, kind, threshold):
    """閾値は実験仕様に保存。最適値の自動探索は行わない。"""
    if kind not in ('drop', 'doji', 'rsi', 'all') or not math.isfinite(threshold):
        raise ValueError('invalid_signal')
    if kind == 'drop' and not 0 < threshold < 1:
        raise ValueError('drop_threshold_out_of_range')
    if kind == 'doji' and not 0 <= threshold <= 1:
        raise ValueError('doji_threshold_out_of_range')
    if kind == 'rsi' and not 0 <= threshold <= 100:
        raise ValueError('rsi_threshold_out_of_range')
    found = []
    for i, row in enumerate(rows):
        if kind == 'all':
            hit = True
        elif kind == 'drop':
            hit = i > 0 and row['close'] / rows[i-1]['close'] - 1 <= -threshold
        elif kind == 'doji':
            spread = row['high'] - row['low']
            hit = spread > 0 and abs(row['close'] - row['open']) / spread <= threshold
        else:
            value = rsi([x['close'] for x in rows[:i+1]])
            hit = value is not None and value <= threshold
        if hit:
            found.append(row['date'])
    return found


def wilson(wins, n):
    """勝率の95%区間。独立試行の近似で、相場の依存性は解消しない。"""
    if not n:
        return None
    z = 1.959963984540054
    p = wins / n
    center = (p + z*z / (2*n)) / (1 + z*z/n)
    half = z * math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / (1+z*z/n)
    return [center-half, center+half]


def summary(trades):
    if not trades:
        return {'n': 0}
    values = [x['net_return'] for x in trades]
    best = max(values)
    return {'n': len(values), 'win_rate': sum(x > 0 for x in values)/len(values),
            'win_rate_95pct_wilson': wilson(sum(x > 0 for x in values), len(values)),
            'mean_net': statistics.mean(values), 'median_net': statistics.median(values),
            'worst_net': min(values), 'best_net': best,
            'mean_without_best': (sum(values)-best)/(len(values)-1) if len(values)>1 else None,
            'mean_benchmark_net': statistics.mean(x['benchmark_net'] for x in trades),
            'mean_excess_net': statistics.mean(x['net_return']-x['benchmark_net'] for x in trades),
            'worst_adverse_excursion': min(x['adverse_excursion'] for x in trades),
            'median_low_session': statistics.median(x['low_session'] for x in trades)}


def event_study(rows, benchmark, sessions, event_dates, *, horizon, cost_bps=25,
                allow_overlap=False, test_start=None):
    """欠損のある窓は除外し、何件落としたかも返す。営業日を行番号で偽装しない。"""
    if horizon < 1 or horizon > 252 or not 0 <= cost_bps < 10000:
        raise ValueError('invalid_horizon_or_cost')
    if len(set(sessions)) != len(sessions) or sessions != sorted(sessions):
        raise ValueError('invalid_session_sequence')
    if len(set(event_dates)) != len(event_dates):
        raise ValueError('duplicate_event_dates')
    own = {r['date']: r for r in rows}
    ref = {r['date']: r for r in benchmark}
    positions = {d: i for i, d in enumerate(sessions)}
    trades, excluded = [], {}
    busy_until = -1
    fee = cost_bps / 10000
    for day in sorted(event_dates):
        i = positions.get(day)
        reason = None
        if i is None:
            reason = 'event_not_session'
        elif i + horizon >= len(sessions):
            reason = 'future_window_incomplete'
        elif not allow_overlap and i + 1 <= busy_until:
            reason = 'overlapping_position'
        else:
            window = sessions[i+1:i+horizon+1]
            if any(d not in own or d not in ref for d in [day] + window):
                reason = 'missing_price_or_benchmark'
            elif test_start and window[0] < test_start <= window[-1]:
                reason = 'crosses_train_test_boundary'
        if reason:
            excluded[reason] = excluded.get(reason, 0) + 1
            continue
        entry, exit_day = window[0], window[-1]
        entry_price = own[entry]['open']
        net = own[exit_day]['close'] / entry_price * (1-fee)/(1+fee) - 1
        baseline = ref[exit_day]['close'] / ref[entry]['open'] * (1-fee)/(1+fee) - 1
        lows = [own[d]['low']/entry_price - 1 for d in window]
        highs = [own[d]['high']/entry_price - 1 for d in window]
        trades.append({'signal_date': day, 'entry': entry, 'exit': exit_day,
                       'net_return': net, 'benchmark_net': baseline,
                       'adverse_excursion': min([0]+lows), 'favorable_excursion': max([0]+highs),
                       'low_session': lows.index(min(lows))+1,
                       'sample': 'test' if test_start and entry >= test_start else 'development'})
        busy_until = i+horizon
    return {'summary': summary(trades), 'excluded': excluded, 'trades': trades,
            'by_year': {year: summary([t for t in trades if t['entry'][:4] == year])
                        for year in sorted({t['entry'][:4] for t in trades})},
            'by_sample': {group: summary([t for t in trades if t['sample'] == group])
                          for group in ('development', 'test')},
            'limitations': ['日足による仮想約定。配当・税・資金制約・借入金利は含まない。',
                           '重複除外後も相場の時系列依存性が残る。勝率区間は近似。',
                           '現在選んだ銘柄の検証であり、上場廃止銘柄を含む全市場の成績ではない。',
                           'Wilder RSIと一般的な小十字定義。特定の筆者の独自条件を再現した意味ではない。']}


def run_study(store, dataset_id, benchmark_id, *, kind, threshold, horizons,
              cost_bps=25, test_start=None, as_of=None, allow_overlap=False):
    own, ref = store.dataset(dataset_id), store.dataset(benchmark_id)
    a, b = own['metadata'], ref['metadata']
    if a.get('calendar_verified') is False or b.get('calendar_verified') is False:
        raise ValueError('unverified_exchange_calendar')
    for key in ('feed', 'adjustment', 'currency', 'timeframe', 'session_scope', 'exchange_timezone'):
        if a.get(key) != b.get(key) or a.get(key) is None:
            raise ValueError('incompatible_price_datasets')
    if a['adjustment'] != 'split' or a['timeframe'] != '1Day':
        raise ValueError('require_split_adjusted_daily_prices')
    # 両銘柄の実在行の共通部分ではなく、取引所のカレンダーを基準にする。
    if a['sessions'] != b['sessions']:
        raise ValueError('calendar_ranges_differ')
    if as_of is None:
        as_of = min(a['end'], b['end'])
    if as_of > min(a['end'], b['end']) or (test_start and test_start > as_of):
        raise ValueError('invalid_study_dates')
    sessions = [d for d in a['sessions'] if d <= as_of]
    rows = [r for r in store.prices(dataset_id) if r['date'] <= as_of]
    bench = [r for r in store.prices(benchmark_id) if r['date'] <= as_of]
    # 欠損の前後を隣り合う日と誤認しないよう、シグナル用の連続区間に分ける。
    segments, segment = [], []
    by_day = {r['date']: r for r in rows}
    for day in sessions:
        if day not in by_day:
            if segment:
                segments.append(segment)
            segment = []
        else:
            segment.append(by_day[day])
    if segment:
        segments.append(segment)
    event_dates = [d for part in segments for d in signals(part, kind, threshold)]
    from pathlib import Path
    import hashlib
    engine_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    specification = dict(engine='market_hub-0.1.0', engine_sha256=engine_hash, dataset_id=dataset_id, benchmark_id=benchmark_id,
                         signal=kind, threshold=threshold, horizons=list(horizons), cost_bps=cost_bps,
                         test_start=test_start, as_of=as_of, allow_overlap=allow_overlap,
                         signal_time='after_daily_bar_finalized', entry='next_session_open',
                         exit='hth_session_close', data_selection='current_selected_symbols')
    result = {'specification': specification, 'events_found': len(event_dates),
              'last_contiguous_segment_indicators': indicators(segments[-1]) if segments else None,
              'studies': {str(h): event_study(rows, bench, sessions, event_dates, horizon=h,
                        cost_bps=cost_bps, test_start=test_start, allow_overlap=allow_overlap) for h in horizons}}
    result['run_id'] = store.save_run(specification, result)
    return result


def adr_premium(*, adr_usd, local_price, local_per_usd, underlying_shares_per_adr,
                adr_at, local_at, fx_at, max_skew_seconds=60):
    """同時点比較だけ許可。終値の時間差をプレミアムに見せかけない。"""
    from .store import timestamp
    values = [adr_usd, local_price, local_per_usd, underlying_shares_per_adr]
    if any(isinstance(x, bool) or not math.isfinite(x) or x <= 0 for x in values) or not 0 <= max_skew_seconds <= 86400:
        raise ValueError('invalid_adr_inputs')
    times = [timestamp(t) for t in (adr_at, local_at, fx_at)]
    skew = (max(times)-min(times)).total_seconds()
    if skew > max_skew_seconds:
        raise ValueError('asynchronous_adr_quotes')
    fair = local_price / local_per_usd * underlying_shares_per_adr
    return {'fair_usd': fair, 'premium': adr_usd/fair-1, 'timestamp_skew_seconds': skew,
            'limitation': 'Fees, tax, conversion restrictions and fungibility are not priced.'}
