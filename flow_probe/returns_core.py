"""通知後の価格損益を計算する。実際の注文を出す機能はない。

日付のずれ、保有中の二重購入、不明値の0扱いを防ぐ計算部分。
価格だけの参考損益と、配当補正による参考値を別々に残す。
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from statistics import mean, median

from .alpaca import NY, numeric

COSTS = (0, 0.0025, 0.005, 0.01)
PRIMARY_COST = 0.0025
HORIZONS = (5, 10, 20)


def after_cost(price_ratio, cost):
    """買付け費用も元手に含める。価格比から往復費用を正確に引く。"""
    if not numeric(price_ratio) or price_ratio <= 0 or not numeric(cost) or not 0 <= cost < 1:
        raise ValueError('invalid_return_or_cost')
    return price_ratio * (1-cost) / (1+cost) - 1


def by_date(bars):
    """銘柄ごとの足を現地日付へ。重複を上書きして隠さない。"""
    output = {}
    for row in bars:
        day = datetime.fromisoformat(row['t'].replace('Z', '+00:00')).astimezone(NY).date().isoformat()
        if day in output: raise ValueError('duplicate_price_day')
        output[day] = row
    return output


def valid_bar(row):
    if not row or not all(numeric(row.get(k)) and row[k] > 0 for k in ('o','h','l','c','v')):
        return False
    return row['l'] <= min(row['o'], row['c']) <= max(row['o'], row['c']) <= row['h']


def schedule(scored, days, field, horizon):
    """通知だけで予定を決める。将来の価格を見て再購入日を変えない。"""
    if days != sorted(set(days)): raise ValueError('invalid_return_calendar')
    index = {d:i for i,d in enumerate(days)}
    output, skipped = [], Counter()
    grouped = defaultdict(list)
    for row in scored:
        if '2026-01-01' <= row['date'] <= '2026-03-31': grouped[row['symbol']].append(row)
    for symbol, rows in sorted(grouped.items()):
        busy_through = -1
        for row in sorted(rows, key=lambda x:x['date']):
            flag = row[field]
            if flag is None: skipped['unknown_signal_days'] += 1; continue
            if not flag: continue
            i = index[row['date']]
            if i <= busy_through: skipped['signal_while_position_reserved'] += 1; continue
            # 次の営業日が購入日。horizon=10ならi+10の日に売却する。
            entry, end = i+1, i+horizon
            available = datetime.fromisoformat(row['date']).replace(tzinfo=NY) + timedelta(days=1, minutes=30)
            output.append({'symbol':symbol,'signal_date':row['date'],
                           'assumed_information_available_after':available.isoformat(),
                           'entry_date':days[entry] if entry<len(days) else None,
                           'exit_date':days[end] if end<len(days) else None,
                           'holding_sessions':horizon,'model':field})
            busy_through = end
    return sorted(output,key=lambda x:(x['signal_date'],x['symbol'])), dict(skipped)


def price_path(book, days, entry, end):
    """評価に必要な全日の足が正常な場合だけ計算する。"""
    window = days[entry:end+1]
    if any(d not in book or not book[d].get('valid') for d in window):
        return None
    buy = book[window[0]]['split']['o']; sell = book[window[-1]]['split']['c']
    return {'ratio':sell/buy,
            'adverse':min(0, min(book[d]['split']['l']/buy-1 for d in window))}


def value_attempt(attempt, books, days, benchmark='SPY'):
    """終値や始値を使う価格診断。執行できた売買とは認定しない。"""
    result = {**attempt,'status':'unresolved','reason':None}
    if not attempt['entry_date'] or not attempt['exit_date']:
        result['reason']='exit_beyond_available_calendar'; return result
    i, end = days.index(attempt['entry_date']), days.index(attempt['exit_date'])
    book = books.get(attempt['symbol'], {})
    path = price_path(book, days, i, end)
    if path is None:
        result['reason']='missing_or_unreviewed_holding_window'; return result
    ratio = path['ratio']
    result.update(status='priced', price_return=ratio-1, net_return=after_cost(ratio,PRIMARY_COST),
                  cost_returns={str(c):after_cost(ratio,c) for c in COSTS},
                  max_adverse_price_return=path['adverse'])
    # 配当補正はあくまで提供元の参考値。実際の配当入金を推測して加算しない。
    a,b = book[attempt['entry_date']].get('dividend'),book[attempt['exit_date']].get('dividend')
    result['dividend_adjusted_reference_return'] = b['c']/a['o']-1 if a and b else None
    result['dividend_adjusted_reference_net'] = after_cost(b['c']/a['o'],PRIMARY_COST) if a and b else None
    bench = price_path(books.get(benchmark,{}),days,i,end)
    result['benchmark_net_return'] = after_cost(bench['ratio'],PRIMARY_COST) if bench else None
    result['net_excess_over_benchmark'] = result['net_return']-result['benchmark_net_return'] if bench else None
    # 以下は失敗原因の診断用。今回の購入可否には使わない。
    d = attempt['signal_date']; j = days.index(d); s=book.get(d)
    previous = book.get(days[j-5]) if j>=5 else None
    valid_signal = s and s.get('valid')
    result['pre_signal_five_day_return'] = s['split']['c']/previous['split']['c']-1 if valid_signal and previous and previous.get('valid') else None
    result['entry_gap'] = book[attempt['entry_date']]['split']['o']/s['split']['c']-1 if valid_signal else None
    span=s['split']['h']-s['split']['l'] if valid_signal else 0
    result['signal_close_position']=(s['split']['c']-s['split']['l'])/span if span>0 else None
    return result


def evaluate(attempts, books, days):
    """前の予定売買の状態が不明なら、その後も成績を積み上げない。"""
    blocked, output = set(), []
    for attempt in attempts:
        if attempt['symbol'] in blocked:
            row={**attempt,'status':'unresolved','reason':'prior_position_state_unresolved'}
        else:
            row=value_attempt(attempt,books,days)
            if row['status']!='priced': blocked.add(attempt['symbol'])
        output.append(row)
    return output


def summarize(rows):
    priced=[r for r in rows if r['status']=='priced']; values=[r['net_return'] for r in priced]
    positive=[v for v in values if v>0]; negative=[v for v in values if v<0]
    result={'attempts':len(rows),'priced':len(priced),'unresolved':len(rows)-len(priced),
            'unresolved_reasons':dict(Counter(r['reason'] for r in rows if r['status']!='priced')),
            'symbols':len({r['symbol'] for r in priced}),
            'mean_net_return':mean(values) if values else None,
            'median_net_return':median(values) if values else None,
            'net_win_fraction':len(positive)/len(values) if values else None,
            'mean_win':mean(positive) if positive else None,'mean_loss':mean(negative) if negative else None,
            'worst_net_return':min(values) if values else None,
            'worst_adverse_price_return':min(r['max_adverse_price_return'] for r in priced) if priced else None,
            'cost_erased_positive_trades':sum(r['price_return']>0 and r['net_return']<=0 for r in priced),
            'mean_price_return':mean(r['price_return'] for r in priced) if priced else None,
            'cost_sensitivity':{str(c):mean(r['cost_returns'][str(c)] for r in priced) if priced else None for c in COSTS}}
    paired=[r for r in priced if r['net_excess_over_benchmark'] is not None]
    dividends=[r for r in priced if r['dividend_adjusted_reference_net'] is not None]
    result.update(benchmark_pairs=len(paired),
                  mean_net_excess_over_benchmark=mean(r['net_excess_over_benchmark'] for r in paired) if paired else None,
                  dividend_reference_pairs=len(dividends),
                  mean_dividend_adjusted_reference_net=mean(r['dividend_adjusted_reference_net'] for r in dividends) if dividends else None,
                  dividend_adjustment_changes_return=sum(abs(r['dividend_adjusted_reference_net']-r['net_return'])>1e-6 for r in dividends))
    per_symbol=defaultdict(list)
    for row in priced:per_symbol[row['symbol']].append(row['net_return'])
    result['equal_symbol_mean_net_return']=mean(mean(v) for v in per_symbol.values()) if per_symbol else None
    deleted=[mean(r['net_return'] for r in priced if r['symbol']!=s) for s in per_symbol if len(per_symbol)>1]
    result['leave_one_symbol_out']={'minimum_mean':min(deleted) if deleted else None,'maximum_mean':max(deleted) if deleted else None,
                                    'interpretation':'fragility_only_not_confidence_interval'}
    result['without_best_trade_mean']=mean(sorted(values)[:-1]) if len(values)>1 else None
    return result


def failure_slices(rows):
    """0・値幅の半分という固定境界で見る。良い境界の検索はしない。"""
    output={}
    for field,cut in [('pre_signal_five_day_return',0),('entry_gap',0),('signal_close_position',0.5)]:
        known=[r for r in rows if r['status']=='priced' and r.get(field) is not None]
        output[field]={'above':summarize([r for r in known if r[field]>cut]),
                       'at_or_below':summarize([r for r in known if r[field]<=cut]),
                       'unknown':len(rows)-len(known),'cut':cut}
    output['months']={m:summarize([r for r in rows if r['signal_date'].startswith(m)]) for m in ['2026-01','2026-02','2026-03']}
    return output


def wait_pairs(first_attempts, scored, books, days):
    """後に反復した例だけの補助比較。最初に選べた取引としては計上しない。"""
    by_symbol=defaultdict(dict)
    for row in scored:by_symbol[row['symbol']][row['date']]=row
    pairs=[];counts=Counter()
    for attempt in first_attempts:
        source=by_symbol[attempt['symbol']];first=source[attempt['signal_date']]
        if first['repeated'] is not False:counts['already_repeated_or_unknown']+=1;continue
        i=days.index(attempt['signal_date'])
        later=next((d for d in days[i+1:i+5] if source.get(d,{}).get('repeated') is True),None)
        if later is None:counts['no_observed_repeat_in_next_four_sessions']+=1;continue
        j=days.index(later)
        delayed={**attempt,'model':'paired_delayed','signal_date':later,'entry_date':days[j+1],
                 'exit_date':days[j+10] if j+10<len(days) else None,
                 'assumed_information_available_after':(datetime.fromisoformat(later).replace(tzinfo=NY)+timedelta(days=1,minutes=30)).isoformat()}
        early=value_attempt(attempt,books,days);late=value_attempt(delayed,books,days)
        if early['status']!='priced' or late['status']!='priced':counts['unpriced_pair']+=1;continue
        pairs.append({'symbol':attempt['symbol'],'first_signal':attempt['signal_date'],'repeat_signal':later,
                      'wait_sessions':j-i,'early_net':early['net_return'],'delayed_net':late['net_return'],
                      'difference_delayed_minus_early':late['net_return']-early['net_return']})
    return {'n':len(pairs),'counts':dict(counts),'pairs':pairs,
            'mean_early_net':mean(r['early_net'] for r in pairs) if pairs else None,
            'mean_delayed_net':mean(r['delayed_net'] for r in pairs) if pairs else None,
            'mean_delayed_minus_early':mean(r['difference_delayed_minus_early'] for r in pairs) if pairs else None,
            'limitation':'conditional_on_future_repetition; overlapping_counterfactuals_not_independent_executable_strategy'}
