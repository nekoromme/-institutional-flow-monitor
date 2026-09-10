"""原価格と確認済みの株数変化で、売買と現金買収への権利を評価する。

これは同額・端数株の価格診断。実際の入金日や端数処理を推測しない。
未来の企業行動は過去の通知条件には入れず、単位換算と会計にだけ使う。
"""
import math
from collections import Counter
from flow_probe.returns_core import by_date, valid_bar, after_cost, COSTS, PRIMARY_COST


def shares_between(events,symbol,start,end):
    multiplier=1.0
    selected=sorted([e for e in events if e['symbol']==symbol and e['kind']=='split' and start<e['effective_date']<=end],key=lambda e:e['effective_date'])
    seen=set()
    for event in selected:
        if event['effective_date'] in seen:raise ValueError('duplicate_split_event')
        seen.add(event['effective_date'])
        ratio=event['new_shares_per_old_share']
        if not isinstance(ratio,(int,float)) or isinstance(ratio,bool) or not math.isfinite(ratio) or ratio<=0:
            raise ValueError('invalid_documented_split_ratio')
        if not event.get('source_sha256'):raise ValueError('split_without_reviewed_source')
        multiplier*=ratio
    return multiplier


def prepare_prices(payload,events):
    books={};quality=[];through=payload['retrieved_at_utc'][:10]
    for symbol,raw in payload['prices']['raw'].items():
        original=by_date(raw);adjusted=by_date(payload['prices']['split'][symbol]);book={};issues=Counter();details=[]
        for day,row in original.items():
            other=adjusted.get(day)
            valid=valid_bar(row) and valid_bar(other)
            factor=1/shares_between(events,symbol,day,through)
            if valid:
                # 共通の診断で既に用いている丸め許容幅を維持。成績を見て広げない。
                valid=all(math.isclose(other[k],row[k]*factor,rel_tol=1e-8,abs_tol=.0001*(factor+1)) for k in ('o','h','l','c'))
                valid=valid and math.isclose(other['v'],row['v']/factor,rel_tol=1e-6,abs_tol=1.0)
            reason=None if valid else 'unreviewed_adjustment_or_invalid_bar'
            if reason:
                issues[reason]+=1
                if valid_bar(row) and valid_bar(other):
                    price_errors={k:abs(other[k]-row[k]*factor) for k in ('o','h','l','c')}
                    details.append({'date':day,'expected_price_factor':factor,'price_absolute_errors':price_errors,
                                    'volume_absolute_error':abs(other['v']-row['v']/factor),
                                    'price_tolerance':.0001*(factor+1),'volume_tolerance':max(1.0,1e-6*max(other['v'],row['v']/factor))})
            book[day]={'raw':row,'valid':bool(valid),'reason':reason}
        if original.keys()!=adjusted.keys():
            for x in book.values():x.update(valid=False,reason='unpaired_dates')
        books[symbol]=book
        quality.append({'symbol':symbol,'observed_days':len(book),'valid_days':sum(x['valid'] for x in book.values()),'issues':dict(issues),'adjustment_failure_diagnostics':details})
    return books,quality


def window_valid(book,dates):
    return bool(dates) and all(book.get(d,{}).get('valid') for d in dates)


def trade_value(attempt,books,days,events):
    result={**attempt,'status':'unresolved','reason':None}
    symbol=attempt['symbol'];entry=attempt['entry_date'];end=attempt['exit_date'];book=books.get(symbol,{})
    if not entry or not end:return {**result,'reason':'exit_beyond_calendar'}
    cashes=[e for e in events if e['symbol']==symbol and e['kind']=='cash_merger_trading_end']
    if len(cashes)>1:raise ValueError('multiple_unresolved_cash_events')
    cash=cashes[0] if cashes else None
    if cash and entry>=cash['first_non_trading_date']:
        return {**result,'status':'not_executable','reason':'ordinary_trading_ended_before_entry'}
    cash_exit=bool(cash and entry<cash['first_non_trading_date']<=end)
    valued_on=cash['first_non_trading_date'] if cash_exit else end
    dates=[d for d in days if entry<=d<=valued_on and (not cash_exit or d<valued_on)]
    if not window_valid(book,dates):return {**result,'reason':'missing_or_unreviewed_holding_window'}
    bought=book[entry]['raw']['o']
    if cash_exit:
        amount=cash['cash_per_share_usd']
        if not math.isfinite(amount) or amount<=0 or not cash.get('source_sha256'):raise ValueError('invalid_cash_event')
        ratio=amount*shares_between(events,symbol,entry,valued_on)/bought
    else:ratio=book[end]['raw']['c']*shares_between(events,symbol,entry,end)/bought
    adverse=min([0]+[book[d]['raw']['l']*shares_between(events,symbol,entry,d)/bought-1 for d in dates])
    if cash_exit:adverse=min(adverse,ratio-1)
    return {**result,'status':'priced','reason':None,'price_return':ratio-1,
            'net_return':after_cost(ratio,PRIMARY_COST),'cost_returns':{str(c):after_cost(ratio,c) for c in COSTS},
            'max_adverse_price_return':adverse,'valuation_date':valued_on,
            'valuation_kind':'cash_entitlement' if cash_exit else 'scheduled_close',
            'cash_payment_date':None,'actual_execution_confirmed':False,
            'cash_dividends_included':False,'cash_exit_cost_assumption':'same_conservative_exit_cost' if cash_exit else None}


def evaluate(attempts,books,days,events):
    results=[];blocked=set()
    for attempt in attempts:
        if attempt['symbol'] in blocked:
            row={**attempt,'status':'unresolved','reason':'prior_position_state_unresolved'}
        else:
            row=trade_value(attempt,books,days,events)
            if row['status']=='unresolved':blocked.add(attempt['symbol'])
        results.append(row)
    return results
