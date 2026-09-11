"""見た期間の利益率を目的に、上位2購入条件の出口を追加調整する。"""
import json
import os
from flow_probe.bulk13f import digest
from .collect import ROOT,write_json,encrypt_bytes,decrypt_bytes
from .exit_search import load_inputs,simulate_exit,paired_index


def run(root,secret):
    scored,books,days,overlap=load_inputs(root,secret)
    initial=json.loads((root/'research/history/evidence/exit-search-initial-results.json').read_text())
    known={r['id']:r for r in initial['results'] if r['status']=='ok'}
    out={'code_commit':os.environ.get('GITHUB_SHA'),
        'plan_sha256':digest(root/'research/history/EXIT_REFINEMENT_PLAN.md'),
        'reserved_2025_opened':False,'scope':'profit_optimized_on_seen_2023_and_2024',
        'new_market_requests':0,'overlap_checked':overlap,'results':[],'reproduced_ids':[]}
    private={}
    def execute(config,stage):
        ident=f"{config['entry']}_h{config['horizon']}_{config['policy']}_a{config['ticket']}_c{config['cost']}"
        try:
            result,record=simulate_exit(scored,books,days,config['entry'],config['horizon'],config['policy'],ticket=config['ticket'],cost=config['cost'])
            row={'id':ident,'config':config,'stage':stage,'status':'ok',**result};private[ident]=record
            if ident in known:
                if row['summary']!=known[ident]['summary'] or row['years']!=known[ident]['years']:
                    raise RuntimeError('previous_result_not_reproduced')
                out['reproduced_ids'].append(ident)
        except ValueError as exc:
            row={'id':ident,'config':config,'stage':stage,'status':'failed','error':str(exc)}
        out['results'].append(row)
    for field in ('depressed','volume_fade'):
        for horizon in (10,15,20,25):
            for gain in (15,20,25):
                for ticket in (.1,.2):
                    execute({'entry':field,'horizon':horizon,'policy':f'profit{gain}','ticket':ticket,'cost':.0025},'refinement')
    ranked=sorted([r for r in out['results'] if r['status']=='ok'],key=lambda r:(-r['summary']['annualized_return'],r['id']))
    out['best_profit_id']=ranked[0]['id'] if ranked else None
    restricted=[r for r in ranked if r['summary']['max_close_drawdown']>=-.2 and all(y['annual_return']>0 for y in r['years'])]
    out['best_positive_years_drawdown20_id']=restricted[0]['id'] if restricted else None
    for r in ranked[:3]:
        r['paired_SPY']=paired_index(private[r['id']],books,days,r['config']['ticket'],r['config']['cost'])
        execute({**r['config'],'cost':.005},'higher_cost')
    # 初回の価格不足を購入条件ごとに1例ずつ特定。既に失敗した設定の診断再実行。
    out['initial_failed_price_locations'] = {}
    for old in initial['results']:
        config = old['config']
        if old['status'] == 'failed' and config['entry'] not in out['initial_failed_price_locations']:
            try:
                simulate_exit(scored, books, days, config['entry'], config['horizon'], config['policy'], ticket=config['ticket'], cost=config['cost'])
                out['initial_failed_price_locations'][config['entry']] = 'unexpected_success'
            except ValueError as exc:
                out['initial_failed_price_locations'][config['entry']] = str(exc)
    out['configuration_count']=len(out['results'])
    raw=json.dumps(private,sort_keys=True,allow_nan=False).encode();enc=encrypt_bytes(raw,secret)
    assert decrypt_bytes(enc,secret)==raw
    path=root/'data/history/encrypted/exit-refinement.enc';path.write_bytes(enc);out['encrypted_sha256']=digest(path)
    write_json(root/'diagnostics/history/exit-refinement/results.json',out)


if __name__=='__main__':run(ROOT,os.environ.get('ALPACA_SECRET_KEY',''))
