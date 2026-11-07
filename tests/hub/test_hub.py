"""値段の例はすべて人工データ。投資成績の証拠にはしない。"""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from market_hub.store import Store, validate_bars
from market_hub.analysis import rsi, event_study, run_study, adr_premium
from market_hub.providers import collect_prices, collect_macro
from market_hub.__main__ import main, import_evidence, cycle

DAYS = ['2024-01-02','2024-01-03','2024-01-04','2024-01-05','2024-01-08','2024-01-09']


def bars(prices=None):
    return [{'date': d, 'open': c, 'high': c+2, 'low': c-2, 'close': c+1, 'volume':1000}
            for d,c in zip(DAYS, prices or [100,90,92,95,96,98])]


def metadata():
    return dict(sessions=DAYS,start=DAYS[0],end=DAYS[-1],retrieved_at_utc='2024-02-01T00:00:00Z',
                currency='USD',feed='sip',adjustment='split',timeframe='1Day',
                session_scope='provider_daily_not_rth_only',exchange_timezone='America/New_York')


class StudyTests(unittest.TestCase):
    def test_entry_is_next_open_and_horizon_counts_from_entry(self):
        result = event_study(bars(),bars(),DAYS,[DAYS[0]],horizon=3,cost_bps=25)
        trade = result['trades'][0]
        self.assertEqual((trade['entry'],trade['exit']),(DAYS[1],DAYS[3]))
        self.assertAlmostEqual(trade['net_return'],96/90*.9975/1.0025-1)
        self.assertAlmostEqual(trade['adverse_excursion'],88/90-1)
        self.assertEqual(trade['low_session'],1)
        self.assertAlmostEqual(result['summary']['mean_excess_net'],0)

    def test_missing_middle_session_is_excluded(self):
        own = [r for r in bars() if r['date'] != DAYS[2]]
        result = event_study(own,bars(),DAYS,[DAYS[0]],horizon=3)
        self.assertEqual(result['summary']['n'],0)
        self.assertEqual(result['excluded'],{'missing_price_or_benchmark':1})

    def test_no_overlap_and_incomplete_future_are_reported(self):
        result = event_study(bars(),bars(),DAYS,DAYS,horizon=2)
        self.assertEqual(result['summary']['n'],2)
        self.assertEqual(result['excluded']['overlapping_position'],2)
        self.assertEqual(result['excluded']['future_window_incomplete'],2)

    def test_holdout_boundary_is_not_crossed(self):
        result = event_study(bars(),bars(),DAYS,[DAYS[0]],horizon=3,test_start=DAYS[3])
        self.assertEqual(result['excluded'],{'crosses_train_test_boundary':1})

    def test_rsi_extremes_and_insufficient_history(self):
        self.assertIsNone(rsi([1]*14))
        self.assertEqual(rsi(list(range(1,30))),100)
        self.assertEqual(rsi(list(range(30,1,-1))),0)
        self.assertEqual(rsi([10]*30),50)

    def test_adr_requires_synchronized_prices(self):
        kwargs = dict(adr_usd=11,local_price=14000,local_per_usd=1400,underlying_shares_per_adr=1,
                      adr_at='2024-01-02T10:00:00Z',local_at='2024-01-02T10:00:00Z',fx_at='2024-01-02T10:00:00Z')
        self.assertAlmostEqual(adr_premium(**kwargs)['premium'],.1)
        with self.assertRaisesRegex(ValueError,'asynchronous'):
            adr_premium(**{**kwargs,'local_at':'2024-01-02T01:00:00Z'})


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dbpath = Path(self.tmp.name)/'book.sqlite3'
        self.store = Store(self.dbpath)
    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()
    def test_versions_integrity_and_deterministic_study(self):
        a = self.store.put('fixture','A',metadata(),bars(),bars())
        b = self.store.put('fixture','B',metadata(),bars(),bars())
        result = run_study(self.store,a,b,kind='drop',threshold=.05,horizons=[1,3])
        self.assertEqual(result['run_id'],run_study(self.store,a,b,kind='drop',threshold=.05,horizons=[1,3])['run_id'])
        self.assertEqual(result['events_found'],1)
        self.store.db.execute('UPDATE bars SET close=123 WHERE dataset_id=? AND date=?',(a,DAYS[0]))
        with self.assertRaisesRegex(ValueError,'integrity'):
            self.store.prices(a)
    def test_invalid_batch_rolls_back(self):
        invalid = bars();invalid[0]['low'] = 999
        with self.assertRaises(ValueError):
            self.store.put('fixture','A',metadata(),invalid,invalid)
        self.assertEqual(self.store.inventory(),[])
    def test_mismatched_feeds_are_not_combined(self):
        a = self.store.put('fixture','A',metadata(),bars(),bars())
        meta = metadata();meta['feed']='iex'
        b = self.store.put('fixture','B',meta,bars(),bars())
        with self.assertRaisesRegex(ValueError,'incompatible'):
            run_study(self.store,a,b,kind='all',threshold=0,horizons=[1])
    def test_missing_session_does_not_create_false_drop(self):
        rows = [r for r in bars() if r['date'] != DAYS[1]]
        a = self.store.put('fixture','A',metadata(),rows,rows)
        b = self.store.put('fixture','B',metadata(),bars(),bars())
        result = run_study(self.store,a,b,kind='drop',threshold=.05,horizons=[1])
        self.assertEqual(result['events_found'],0)
    def test_macro_pins_vintage_and_paginates(self):
        calls=[]
        def fetch(params):
            calls.append(dict(params))
            return {'count':2,'observations':[{'date':DAYS[params['offset']],'value':'1.0'}]}
        with patch.dict(os.environ,{'FRED_API_KEY':'test-only'}):
            data = collect_macro(self.store,'DGS10',DAYS[0],DAYS[-1],'2024-02-01',fetch=fetch)
        self.assertEqual(len(calls),2)
        self.assertTrue(all(x['realtime_start']==x['realtime_end']=='2024-02-01' for x in calls))
        self.assertNotIn('test-only',json.dumps(self.store.dataset(data)))
    def test_evidence_requires_timezone_and_publication_order(self):
        p=Path(self.tmp.name)/'evidence.json'
        p.write_text(json.dumps(dict(source='fixture',symbol='A',source_url='https://example.com',
            published_at='2024-01-02T10:00:00Z',observed_at='2024-01-01T10:00:00Z',
            retrieved_at_utc='2024-01-02T09:00:00Z',measurement='estimated',coverage='one venue',value=1)))
        with self.assertRaisesRegex(ValueError,'publication'):
            import_evidence(self.store,p)
    def test_default_cycle_generates_only_aggregate_public_report(self):
        repo = Path(__file__).resolve().parents[2]
        def fake_collect(store, symbols, start, end):
            output=[]
            for symbol in symbols:
                data=store.put('alpaca',symbol,metadata(),bars(),bars())
                output.append({'symbol':symbol,'dataset_id':data,'rows':6,'missing_sessions':[]})
            return output
        request=json.loads((repo/'config/research-request.json').read_text())
        for experiment in request['experiments']:
            experiment['test_start']='2024-01-05'
        request_path=Path(self.tmp.name)/'request.json'
        request_path.write_text(json.dumps(request))
        original = Path.cwd()
        try:
            os.chdir(self.tmp.name)
            with patch('market_hub.__main__.collect_prices',side_effect=fake_collect):
                result=cycle(self.store,repo/'config/market-hub.json',request_path)
            self.assertEqual(result['symbols'],22)
            public=json.loads(Path('diagnostics/hub/study-summary.json').read_text())
            self.assertEqual(len(public['experiments']),3)
            for experiment in public['experiments']:
                for report in experiment['studies'].values():
                    self.assertNotIn('trades',report)
            self.assertNotIn('\"close\":',json.dumps(public))
        finally:
            os.chdir(original)

    def test_encrypted_pack_restore(self):
        self.store.put('fixture','A',metadata(),bars(),bars())
        encrypted=Path(self.tmp.name)/'book.enc'
        restored=Path(self.tmp.name)/'restored.sqlite3'
        with patch.dict(os.environ,{'ALPACA_SECRET_KEY':'fixture-secret-32-characters-long'}):
            self.assertEqual(main(['--db',str(self.dbpath),'pack','--file',str(encrypted)]),0)
            self.assertEqual(main(['--db',str(restored),'unpack','--file',str(encrypted)]),0)
            other = Store(restored)
            self.assertEqual(len(other.inventory()),1)
            other.close()
        self.assertNotIn(b'2024-01-02',encrypted.read_bytes())


class FakeAlpaca:
    def __init__(self,partial=False):
        self.params=[];self.partial=partial
    def json(self,url,params):
        self.params.append(dict(params))
        if 'calendar' in url:
            return [{'date':d} for d in DAYS]
        page = params.get('page_token')
        if self.partial and page:
            from flow_probe.http_client import ProbeError
            raise ProbeError('fixture_failure')
        selected = bars()[:3] if not page else bars()[3:]
        return {'bars':{'A':[dict(t=r['date']+'T05:00:00Z',o=r['open'],h=r['high'],l=r['low'],c=r['close'],v=r['volume']) for r in selected]},
                'next_page_token':None if page else 'second'}
    def metrics(self):
        return dict(received_bytes=0,http_requests=len(self.params),retries=0)


class ProviderTests(unittest.TestCase):
    def test_pages_and_explicit_sip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'db');client=FakeAlpaca()
            result=collect_prices(store,['A'],DAYS[0],DAYS[-1],client=client)
            self.assertEqual(result[0]['rows'],6)
            self.assertTrue(all(p['feed']=='sip' for p in client.params[1:]))
            store.close()
    def test_partial_download_never_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'db')
            with self.assertRaisesRegex(ValueError,'incomplete'):
                collect_prices(store,['A'],DAYS[0],DAYS[-1],client=FakeAlpaca(True))
            self.assertEqual(store.inventory(),[])
            store.close()
