"""共通CLI。失敗時に秘密値やAPI応答を表示しない。詳しい結果はローカルへ。"""
import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime, timedelta
from flow_probe.http_client import ProbeError
from .store import Store, canonical, utcnow
from .providers import collect_prices, collect_sec, collect_macro
from .analysis import run_study


def write_private(path, value):
    """価格・分析結果はdata配下へ。書きかけを完成品として残さない。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    temporary.replace(path)


def parser():
    root = argparse.ArgumentParser(description='株価研究の共通環境。出力の既定保存先は非公開データ領域。')
    root.add_argument('--db', default='data/hub/market.sqlite3')
    commands = root.add_subparsers(dest='command', required=True)
    fetch = commands.add_parser('fetch', help='Alpacaから確定済みの日足を取得')
    fetch.add_argument('--symbols', required=True)
    fetch.add_argument('--start', required=True)
    fetch.add_argument('--end', required=True)
    sec = commands.add_parser('sec', help='SEC提出履歴とXBRLを取得')
    sec.add_argument('--cik', required=True)
    macro = commands.add_parser('macro', help='当時の改定版を指定してFREDを取得')
    for arg in ('series','start','end','vintage'):
        macro.add_argument('--'+arg, required=True)
    enrich = commands.add_parser('enrich', help='韓国株・公式経済CSV・オプション・主体別指標の取得')
    enrich.add_argument('--config', default='config/source-request.json')
    history = commands.add_parser('option-history', help='指定契約の過去オプションを取得')
    history.add_argument('--contracts',required=True)
    history.add_argument('--start',required=True)
    history.add_argument('--end',required=True)
    history.add_argument('--kind',choices=['bars','trades'],default='bars')
    history.add_argument('--feed',choices=['opra','indicative'])
    commands.add_parser('inventory', help='保存済みデータのIDと取得時刻を表示')
    study = commands.add_parser('study', help='条件固定のイベント検証')
    study.add_argument('--dataset', required=True)
    study.add_argument('--benchmark', required=True)
    study.add_argument('--signal', choices=['drop','doji','rsi','all'], required=True)
    study.add_argument('--threshold', type=float, required=True)
    study.add_argument('--horizons', default='1,3,5,10,20')
    study.add_argument('--cost-bps', type=float, default=25)
    study.add_argument('--test-start')
    study.add_argument('--as-of')
    study.add_argument('--allow-overlap', action='store_true')
    study.add_argument('--output', default='data/hub/latest-study.json')
    imp = commands.add_parser('import-prices', help='契約済みデータ等のCSVを取り込み')
    imp.add_argument('--csv', required=True)
    imp.add_argument('--metadata', required=True)
    ev = commands.add_parser('import-evidence', help='ニュース・決算・主体別売買等のJSON記録を取り込み')
    ev.add_argument('--json', required=True)
    cycle = commands.add_parser('cycle', help='監視銘柄を収集し、品質診断だけ公開')
    cycle.add_argument('--config', default='config/market-hub.json')
    cycle.add_argument('--request', default='config/research-request.json')
    pack = commands.add_parser('pack', help='ローカルDBを暗号化して保存')
    pack.add_argument('--file', default='data/hub/encrypted/hub.enc')
    unpack = commands.add_parser('unpack', help='暗号化済みDBを復元。既存DBの上書きは拒否')
    unpack.add_argument('--file', default='data/hub/encrypted/hub.enc')
    return root


def import_prices(store, path, meta_path):
    metadata = json.loads(Path(meta_path).read_text())
    required = ('source','symbol','source_url','retrieved_at_utc','currency','exchange_timezone',
                'feed','adjustment','timeframe','session_scope','sessions','start','end')
    if any(not metadata.get(k) for k in required):
        raise ValueError('price_metadata_missing')
    with Path(path).open(newline='') as handle:
        raw = list(csv.DictReader(handle))
    rows = [{'date': r['date'], **{k: float(r[k]) for k in ('open','high','low','close','volume')}} for r in raw]
    return store.put(metadata['source'], metadata['symbol'], metadata, raw, rows)


def import_evidence(store, path):
    from .store import timestamp
    payload = json.loads(Path(path).read_text())
    required = ('source','symbol','source_url','published_at','observed_at','retrieved_at_utc',
                'measurement','coverage','value')
    if any(k not in payload for k in required):
        raise ValueError('evidence_metadata_missing')
    # published=知り得た時刻、observed=売買や決算が対象とする時刻。混同しない。
    for key in ('published_at','observed_at','retrieved_at_utc'):
        timestamp(payload[key])
    if timestamp(payload['published_at']) > timestamp(payload['retrieved_at_utc']):
        raise ValueError('evidence_retrieved_before_publication')
    if payload['measurement'] not in ('measured','estimated','opinion'):
        raise ValueError('invalid_measurement_class')
    return store.put(payload['source'], payload['symbol'], {k: payload[k] for k in required if k != 'value'}, payload)


def cycle(store, config_path, request_path):
    from .providers import NY
    config = json.loads(Path(config_path).read_text())
    end = datetime.now(NY).date() - timedelta(days=1)
    start = end - timedelta(days=config['lookback_calendar_days'])
    results = collect_prices(store, config['symbols'], start.isoformat(), end.isoformat())
    # 依頼内容もリポジトリで固定し、今後はJSON変更だけで再検証できる。
    request = json.loads(Path(request_path).read_text())
    ids = {r['symbol']:r['dataset_id'] for r in results}
    summaries = []
    for experiment in request['experiments']:
        report = run_study(store, ids[experiment['symbol']], ids[experiment['benchmark']],
            kind=experiment['signal'], threshold=experiment['threshold'],
            horizons=experiment['horizons'], cost_bps=experiment['cost_bps'],
            test_start=experiment.get('test_start'))
        summaries.append({'name': experiment['name'], 'run_id':report['run_id'],
            'specification':report['specification'], 'events_found':report['events_found'],
            'interpretation':'exploratory; declared date split is not proof of untouched holdout',
            'studies':{h:{k:v for k,v in r.items() if k != 'trades'} for h,r in report['studies'].items()}})
    write_private('diagnostics/hub/study-summary.json', {'generated_at_utc':utcnow(), 'experiments':summaries})
    # 現在の取得版だけを保管。研究で参照済みの版は消さない。
    protected = set()
    for row in store.db.execute('SELECT specification FROM runs'):
        spec = json.loads(row['specification'])
        protected.update([spec.get('dataset_id'), spec.get('benchmark_id')])
    newest = {r['dataset_id'] for r in results}
    with store.db:
        for row in store.db.execute("SELECT id FROM datasets WHERE source='alpaca'").fetchall():
            if row['id'] not in protected | newest:
                store.db.execute('DELETE FROM bars WHERE dataset_id=?', (row['id'],))
                store.db.execute('DELETE FROM datasets WHERE id=?', (row['id'],))
    store.db.execute('VACUUM')
    status = {'status': 'ok', 'retrieved_at_utc': utcnow(), 'symbols': len(results),
              'records': sum(r['rows'] for r in results),
              'symbols_with_missing_sessions': sum(bool(r['missing_sessions']) for r in results),
              'raw_data_public': False, 'coverage': 'Alpaca SIP split-adjusted provider daily bars',
              'cache_retention_days': 90,
              'coverage_diagnostics': [{'symbol':r['symbol'], 'rows':r['rows'],
                  'missing_session_count':len(r['missing_sessions'])} for r in results]}
    write_private('diagnostics/hub/status.json', status)
    return status


def main(argv=None):
    args = parser().parse_args(argv)
    store = None
    try:
        if args.command == 'unpack':
            from research.history.collect import decrypt_bytes
            db = Path(args.db)
            if db.exists():
                raise ValueError('restore_would_overwrite_database')
            raw = decrypt_bytes(Path(args.file).read_bytes(), os.environ.get('ALPACA_SECRET_KEY',''))
            if not raw.startswith(b'SQLite format 3\x00'):
                raise ValueError('invalid_database_envelope')
            db.parent.mkdir(parents=True, exist_ok=True)
            db.write_bytes(raw)
            print(canonical({'status':'restored'}))
            return 0
        store = Store(args.db)
        if args.command == 'fetch':
            result = collect_prices(store, args.symbols.split(','), args.start, args.end)
        elif args.command == 'sec':
            result = collect_sec(store, args.cik)
        elif args.command == 'macro':
            result = {'dataset_id': collect_macro(store,args.series,args.start,args.end,args.vintage)}
        elif args.command == 'enrich':
            from .supplemental import collect_supplemental
            result = collect_supplemental(store,args.config)
            write_private('diagnostics/hub/source-status.json',result)
        elif args.command == 'option-history':
            from .options_data import collect_history
            result = collect_history(store,args.contracts.split(','),args.start,args.end,kind=args.kind,feed=args.feed)
        elif args.command == 'inventory':
            result = store.inventory()
        elif args.command == 'import-prices':
            result = {'dataset_id': import_prices(store,args.csv,args.metadata)}
        elif args.command == 'import-evidence':
            result = {'dataset_id': import_evidence(store,args.json)}
        elif args.command == 'study':
            result = run_study(store,args.dataset,args.benchmark,kind=args.signal,threshold=args.threshold,
                horizons=[int(x) for x in args.horizons.split(',')],cost_bps=args.cost_bps,
                test_start=args.test_start,as_of=args.as_of,allow_overlap=args.allow_overlap)
            write_private(args.output,result)
            result = {'run_id':result['run_id'],'status':'saved_private_report'}
        elif args.command == 'cycle':
            result = cycle(store,args.config,args.request)
        elif args.command == 'pack':
            from research.history.collect import encrypt_bytes, decrypt_bytes
            store.db.commit()
            raw = Path(args.db).read_bytes()
            secret = os.environ.get('ALPACA_SECRET_KEY','')
            encrypted = encrypt_bytes(raw,secret)
            if decrypt_bytes(encrypted,secret) != raw:
                raise ValueError('encrypted_roundtrip_failed')
            target = Path(args.file)
            target.parent.mkdir(parents=True,exist_ok=True)
            temporary = target.with_suffix('.tmp')
            temporary.write_bytes(encrypted)
            temporary.replace(target)
            result = {'status':'encrypted','bytes':len(encrypted)}
        print(canonical(result))
        return 0
    except Exception as exc:
        # 未知の例外文字列は外部応答やパスを含み得るため、公開しない。
        category = exc.category if isinstance(exc,ProbeError) else 'hub_operation_failed'
        if isinstance(exc,ValueError) and re.fullmatch(r'[a-z_]{3,80}',str(exc)):
            category = str(exc)
        print(canonical({'status':'error','category':category,'operation':args.command}),file=sys.stderr)
        return 1
    finally:
        if store:
            store.close()


if __name__ == '__main__':
    raise SystemExit(main())
