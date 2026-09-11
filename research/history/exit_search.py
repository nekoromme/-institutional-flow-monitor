"""利益を優先して売却条件を探索する。未知期間の検証とは分けて保存する。"""
import json
import os
from collections import defaultdict, Counter
from statistics import mean
from flow_probe.bulk13f import digest
from flow_probe.returns_core import by_date, valid_bar
from .collect import ROOT, write_json, encrypt_bytes, decrypt_bytes
from .gap_audit import load_expanded
from .practical_validation import prepare
from .development_validation import prepare_signals
from .quiet_trial import quiet_signals
from .downside_trial import add_flags
from .reversal_trial import enrich
from .cohort_context_trial import prior_returns, peer_median
from .annual_continuous import merge_books, annual_summaries, SOURCE_HASH
from .annual_portfolio import simulate

ENTRY_FIELDS = ('downside_allowed','falling_signal','cohort_tailwind','volume_fade','depressed')
POLICIES = ('stop5','stop10','profit10','profit20','trail5','trail10')


def exit_reason(policy, close, entry, peak):
    """終値だけで判定する。日中の高値や翌日の価格を先取りしない。"""
    if policy.startswith('stop') and close/entry-1 <= -int(policy[4:])/100 + 1e-12:
        return 'stop'
    if policy.startswith('profit') and close/entry-1 >= int(policy[6:])/100 - 1e-12:
        return 'profit'
    # 一度5%上がるまでは利益追跡を始めない。初期の下落は最長期間まで持ち得る。
    if policy.startswith('trail') and peak >= entry*1.05-1e-12 and close/peak-1 <= -int(policy[5:])/100+1e-12:
        return 'trailing'
    return None


def simulate_exit(scored, books, days, field, horizon, policy='none', *, ticket=.1, cost=.0025):
    """日々の実際の保有に基づいて購入・売却し、資金不足を隠さない。

    順序は前日判定の寄付き売却→寄付き購入→期限による引け売却→
    残った保有の終値判定。この順序で引け売却の資金を朝に使う誤りを防ぐ。
    """
    index={d:i for i,d in enumerate(days)}
    arrivals=defaultdict(list)
    for r in scored:
        if r.get(field) is True and r['date'] in index:
            i=index[r['date']]+1
            if i<len(days):arrivals[i].append(r)
    cash=1.0;positions={};last_exit={};fills=[];daily=[];closed=[];skipped=Counter()
    peak_equity=1.0;worst=0.0;max_held=0;next_id=0
    limit=round(1/ticket)

    def price(s,d):
        b=books.get(s,{}).get(d)
        if not b or not b['valid']:raise ValueError(f'missing_required_price:{s}:{d}')
        return b['split']

    def sell(s,i,when,why):
        nonlocal cash
        p=positions.pop(s);d=days[i];value=price(s,d)['o' if when=='open' else 'c']
        received=p['quantity']*value*(1-cost);cash+=received;last_exit[s]=i
        fills.append({'id':p['id'],'event':'sell','date':d,'asset':s,'cash':received,'when':when,'reason':why})
        closed.append({'id':p['id'],'symbol':s,'signal_date':p['signal_date'],
            'entry_date':p['entry_date'],'exit_date':d,'exit_when':when,'reason':why,
            'cash_profit':received-ticket,'trade_net_return':received/ticket-1,
            'holding_sessions':i-p['entry_i']+1})

    for i,d in enumerate(days):
        for s,p in list(positions.items()):
            if p['pending'] is not None:sell(s,i,'open',p['pending'])
        for r in sorted(arrivals[i],key=lambda r:r['symbol']):
            s=r['symbol']
            if s in positions:skipped['already_held']+=1;continue
            if i-1 <= last_exit.get(s,-2):skipped['exit_day_signal']+=1;continue
            if len(positions)>=limit:skipped['position_limit']+=1;continue
            if cash+1e-12<ticket:skipped['insufficient_cash']+=1;continue
            buy=price(s,d)['o'];quantity=ticket/((1+cost)*buy);cash-=ticket
            if abs(cash)<1e-12:cash=0.0
            positions[s]={'id':next_id,'entry_i':i,'entry_date':d,'signal_date':r['date'],
                'entry_price':buy,'quantity':quantity,'peak':buy,'pending':None}
            fills.append({'id':next_id,'event':'buy','date':d,'asset':s,'cash':ticket,'when':'open'})
            next_id+=1
        max_held=max(max_held,len(positions))
        for s,p in list(positions.items()):
            if i-p['entry_i']+1 >= horizon:sell(s,i,'close','time_limit')
        for s,p in positions.items():
            close=price(s,d)['c'];p['peak']=max(p['peak'],close)
            p['pending']=exit_reason(policy,close,p['entry_price'],p['peak'])
        stock=sum(p['quantity']*price(s,d)['c'] for s,p in positions.items())
        equity=cash+stock
        if cash < -1e-10 or equity<=0:raise ValueError('invalid_cash_equity')
        peak_equity=max(peak_equity,equity);worst=min(worst,equity/peak_equity-1)
        daily.append({'date':d,'cash':cash,'stock_value':stock,'equity':equity,
            'positions':len(positions),'invested_fraction':stock/equity})
    record={'daily':daily,'fills':fills,'closed':closed,'open_positions':positions}
    summary={'end_equity':daily[-1]['equity'],'two_year_total_return':daily[-1]['equity']-1,
        'annualized_return':daily[-1]['equity']**.5-1,'max_close_drawdown':worst,
        'average_invested_fraction':mean(r['invested_fraction'] for r in daily),
        'max_positions_observed':max_held,'purchases':next_id,'closed_trades':len(closed),
        'mean_holding_sessions':mean(t['holding_sessions'] for t in closed) if closed else None,
        'exit_reasons':dict(Counter(t['reason'] for t in closed)),
        'skipped':dict(skipped),'year_end_open_positions':len(positions),
        'closed_trade_win_fraction':mean(t['trade_net_return']>0 for t in closed) if closed else None}
    return {'summary':summary,'years':annual_summaries(record)},record


def load_inputs(root,secret):
    combined={};all_scored=[];calendar=set();overlap=0
    for year in (2023,2024):
        p=load_expanded(root,year,secret);books,_=prepare(p)
        scored,days=prepare_signals(p,books,[],year)
        quiet_signals(scored,books,days);add_flags(scored,books,days);enrich(scored,books,days)
        index={d:i for i,d in enumerate(days)};cache={}
        for r in scored:
            r['cohort_tailwind']=None
            if r['falling_control'] is None:continue
            d=r['date']
            if d not in cache:cache[d]=prior_returns(books,days,index[d])
            v=peer_median(cache[d],r['symbol'])
            if v is not None:r['cohort_tailwind']=r['falling_signal'] and v>0
        overlap+=merge_books(combined,books);all_scored.extend(scored);calendar.update(days)
    if len(all_scored)!=len({(r['symbol'],r['date']) for r in all_scored}):raise ValueError('duplicate_signals')
    source=root/'data/history/prior-annual/annual-portfolio.enc'
    if digest(source)!=SOURCE_HASH:raise ValueError('benchmark_input_changed')
    captures=json.loads(decrypt_bytes(source.read_bytes(),secret))['benchmark_captures']
    for s in ('SPY','IWM'):
        combined[s]={d:{'valid':valid_bar(b),'split':b} for d,b in by_date(captures['split'][s]).items()}
    return all_scored,combined,[d for d in sorted(calendar) if '2023-01-01'<=d<='2024-12-31'],overlap


def paired_index(record,books,days,ticket,cost):
    """元戦略の実際の購入・売却日で指数を買う事後比較。
    条件付き売却の判定には元銘柄の価格を使ったままなので、独立した指数戦略ではない。
    """
    closes={t['id']:t for t in record['closed']};attempts=[]
    for f in record['fills']:
        if f['event']!='buy':continue
        t=closes.get(f['id'])
        attempts.append({'symbol':'SPY','entry_date':f['date'],
            'exit_date':t['exit_date'] if t else '9999-12-31','exit_when':t['exit_when'] if t else None,'id':f['id']})
    # 同じ元本を各対応取引に割り当てた差を見る。借入可能な指数口座とは扱わない。
    pnl=[]
    for t in attempts:
        b=books['SPY'];entry=b[t['entry_date']]['split']['o']
        last=t['exit_date'] if t['exit_date']!='9999-12-31' else days[-1]
        sell=b[last]['split']['o' if t['exit_when']=='open' else 'c']
        ratio=sell/entry/(1+cost)*(1-cost if t['exit_when'] else 1)
        pnl.append(ticket*(ratio-1))
    return {'paired_trades':len(pnl),'sum_pnl_fraction_initial':sum(pnl),
            'interpretation':'paired_same_notional_not_cash_constrained_index_portfolio'}


def run(root,secret):
    scored,books,days,overlap=load_inputs(root,secret)
    configs=[]
    for field in ENTRY_FIELDS:
        configs.extend({'entry':field,'horizon':h,'policy':'none','ticket':.1,'cost':.0025} for h in (5,10,20,40))
        configs.extend({'entry':field,'horizon':h,'policy':p,'ticket':.1,'cost':.0025} for h in (20,40) for p in POLICIES)
    out={'code_commit':os.environ.get('GITHUB_SHA'),'plan_sha256':digest(root/'research/history/EXIT_SEARCH_PLAN.md'),
        'reserved_2025_opened':False,'scope':'profit_optimized_on_seen_2023_and_2024',
        'new_market_requests':0,'overlap_checked':overlap,'base_configurations':len(configs),'results':[]}
    private={}
    def execute(config,stage):
        ident=f"{config['entry']}_h{config['horizon']}_{config['policy']}_a{config['ticket']}_c{config['cost']}"
        try:
            result,record=simulate_exit(scored,books,days,config['entry'],config['horizon'],config['policy'],ticket=config['ticket'],cost=config['cost'])
            row={'id':ident,'config':config,'stage':stage,'status':'ok',**result}
            private[ident]=record
        except ValueError as exc:
            row={'id':ident,'config':config,'stage':stage,'status':'failed','error':str(exc)}
        out['results'].append(row)
        return row
    for config in configs:execute(config,'base')
    ranked=sorted([r for r in out['results'] if r['status']=='ok'],key=lambda r:(-r['summary']['annualized_return'],r['id']))
    out['base_top_ids']=[r['id'] for r in ranked[:5]]
    # 見た成績の上位をさらに試すことを明記する。未知期間検証とは呼ばない。
    for r in ranked[:5]:
        execute({**r['config'],'ticket':.2},'larger_position')
        execute({**r['config'],'cost':.005},'higher_cost')
    normal=[r for r in out['results'] if r['status']=='ok' and r['config']['cost']==.0025]
    normal.sort(key=lambda r:(-r['summary']['annualized_return'],r['id']))
    out['best_profit_id']=normal[0]['id'] if normal else None
    restricted=[r for r in normal if r['summary']['max_close_drawdown']>=-.2 and all(y['annual_return']>0 for y in r['years'])]
    out['best_positive_years_drawdown20_id']=restricted[0]['id'] if restricted else None
    out['configuration_count']=len(out['results'])
    for r in normal[:5]:r['paired_SPY']=paired_index(private[r['id']],books,days,r['config']['ticket'],r['config']['cost'])
    out['legacy_baseline']=json.loads((root/'research/history/evidence/annual-continuous-results.json').read_text())['models']['cohort_tailwind']
    out['index_buy_hold']=json.loads((root/'research/history/evidence/annual-continuous-results.json').read_text())['models']['SPY_buy_hold']
    raw=json.dumps(private,sort_keys=True,allow_nan=False).encode();enc=encrypt_bytes(raw,secret)
    assert decrypt_bytes(enc,secret)==raw
    path=root/'data/history/encrypted/exit-search.enc';path.write_bytes(enc);out['encrypted_sha256']=digest(path)
    write_json(root/'diagnostics/history/exit-search/results.json',out)
    print(json.dumps({'configurations':out['configuration_count'],'best':out['best_profit_id'],'failures':sum(r['status']=='failed' for r in out['results'])}))


if __name__=='__main__':run(ROOT,os.environ.get('ALPACA_SECRET_KEY',''))
