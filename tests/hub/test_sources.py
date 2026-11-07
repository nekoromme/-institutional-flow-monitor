"""外部取得の境界と意味を検証する。API権限そのものは本番で別途実測。"""
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from flow_probe.http_client import ProbeError
from market_hub.store import Store
from market_hub.source_access import SourceHttp
from market_hub.supplemental import collect_fred_csv, collect_korea_naver, collect_retail_top10
from market_hub.options_data import collect_chain, collect_history


class FakeSource:
    def __init__(self,payloads):
        self.payloads=list(payloads);self.calls=[]
    def read(self,url,params=None):
        self.calls.append((url,dict(params or {})))
        return self.payloads.pop(0)
    def json(self,url,params=None):
        return self.read(url,params)


class SourcesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'db')
    def tearDown(self):
        self.store.close();self.tmp.cleanup()
    def test_fred_current_values_never_claim_point_in_time(self):
        source=FakeSource([b'observation_date,DGS10\n2024-01-02,4.0\n2024-01-03,\n'])
        result=collect_fred_csv(self.store,'DGS10','2024-01-01','2024-01-05',client=source)
        saved=self.store.dataset(result['dataset_id'])
        self.assertFalse(saved['metadata']['point_in_time_usable'])
        self.assertIsNone(saved['payload']['observations'][1]['value'])
        self.assertEqual(result['missing_values'],1)
    def test_monthly_graph_boundary_is_filtered_not_backfilled(self):
        source=FakeSource([b'observation_date,UNRATE\n2024-01-01,4.0\n2024-02-01,4.1\n'])
        result=collect_fred_csv(self.store,'UNRATE','2024-01-20','2024-02-20',client=source)
        saved=self.store.dataset(result['dataset_id'])
        self.assertEqual(result['rows'],1)
        self.assertEqual(saved['metadata']['outside_requested_range_filtered'],1)
        self.assertEqual(saved['payload']['observations'][0]['date'],'2024-02-01')

    def test_fred_wrong_series_or_duplicates_fail(self):
        for raw in [b'DATE,WRONG\n2024-01-02,4\n',b'DATE,DGS10\n2024-01-02,4\n2024-01-02,4\n']:
            with self.assertRaises(ValueError):
                collect_fred_csv(self.store,'DGS10','2024-01-01','2024-01-05',client=FakeSource([raw]))
        self.assertEqual(self.store.inventory(),[])
    def test_naver_halted_prices_quarantined_and_calendar_unverified(self):
        raw=b'<protocol><chartdata symbol="000660" timeframe="day"><item data="20240102|100|110|90|105|1000"/><item data="20240103|0|0|0|105|0"/></chartdata></protocol>'
        result=collect_korea_naver(self.store,'000660',client=FakeSource([raw]))
        self.assertEqual(result['rows'],1)
        self.assertEqual(result['invalid_rows_quarantined'],1)
        saved=self.store.dataset(result['dataset_id'])
        self.assertFalse(saved['metadata']['calendar_verified'])
        self.assertEqual(saved['metadata']['adjustment'],'unknown')
    def test_naver_wrong_entity_rejected(self):
        raw=b'<protocol><chartdata symbol="005930" timeframe="day"/></protocol>'
        with self.assertRaisesRegex(ValueError,'entity'):
            collect_korea_naver(self.store,'000660',client=FakeSource([raw]))
    def test_retail_key_missing_is_not_zero_flow(self):
        with patch.dict(os.environ,{},clear=True):
            result=collect_retail_top10(self.store,'2024-01-01','2024-01-03')
        self.assertEqual(result['status'],'not_configured')
        self.assertNotIn('net_flow',result)
    def test_retail_cursor_and_semantics(self):
        columns=[{'name':x} for x in ['date','ticker','activity','sentiment']]
        source=FakeSource([
            {'datatable':{'columns':columns,'data':[['2024-01-02','MU',.02,5]]},'meta':{'next_cursor_id':'second'}},
            {'datatable':{'columns':columns,'data':[['2024-01-03','SPY',.03,-2]]},'meta':{'next_cursor_id':None}}])
        with patch.dict(os.environ,{'NASDAQ_DATA_LINK_API_KEY':'not-real-fixture-secret'}):
            result=collect_retail_top10(self.store,'2024-01-01','2024-01-03',client=source)
        self.assertEqual(result['rows'],2)
        saved=self.store.dataset(result['dataset_id'])
        self.assertIn('daily_net_buy_dollars',saved['metadata']['not_available'])
        self.assertNotIn('not-real-fixture-secret',str(saved))
    def test_option_current_greeks_are_not_historical_iv(self):
        source=FakeSource([{'snapshots':{'MU261120C00100000':{'greeks':{'delta':.5}}},'next_page_token':None}])
        identifier,groups=collect_chain(self.store,'MU',feed='indicative',client=source)
        metadata=self.store.dataset(identifier)['metadata']
        self.assertTrue(metadata['indicative_derivatives'])
        self.assertFalse(metadata['historical_greeks'])
        self.assertFalse(metadata['open_interest_included'])
    def test_option_quotes_are_paginated_and_feed_explicit(self):
        contract='MU240621C00100000'
        source=FakeSource([{'quotes':{contract:[{'t':'2024-06-03T18:00:01Z','bp':1,'ap':2}]},'next_page_token':'second'},
                           {'quotes':{contract:[{'t':'2024-06-03T18:00:02Z','bp':1,'ap':2}]},'next_page_token':None}])
        result=collect_history(self.store,[contract],'2024-06-03T18:00:00Z','2024-06-03T18:01:00Z',kind='quotes',feed='opra',client=source)
        self.assertEqual(result['records'],2)
        self.assertTrue(all(call[1]['feed']=='opra' for call in source.calls))
    def test_option_history_before_february_2024_refused(self):
        with self.assertRaisesRegex(ValueError,'available_range'):
            collect_history(self.store,['MU240119C00100000'],'2024-01-02T00:00:00Z','2024-01-03T00:00:00Z')
    def test_option_history_empty_is_not_success(self):
        with self.assertRaisesRegex(ValueError,'empty_option_history'):
            collect_history(self.store,['MU240621C00100000'],'2024-06-03T00:00:00Z','2024-06-04T00:00:00Z',client=FakeSource([{'bars':{},'next_page_token':None}]))
    def test_option_repeated_page_token_fails(self):
        contract='MU240621C00100000'
        payload={'bars':{contract:[{'t':'2024-06-03T04:00:00Z','c':1}]},'next_page_token':'same'}
        with self.assertRaisesRegex(ValueError,'repeated'):
            collect_history(self.store,[contract],'2024-06-03T00:00:00Z','2024-06-04T00:00:00Z',client=FakeSource([payload,payload]))
        self.assertEqual(self.store.inventory(),[])


class HttpTests(unittest.TestCase):
    def test_no_orders_accounts_arbitrary_hosts_or_credentials_in_urls(self):
        client=SourceHttp()
        for url in ['https://paper-api.alpaca.markets/v2/account','https://data.alpaca.markets/v2/orders',
                    'https://example.com','https://data.alpaca.markets:444/v1beta1/options/bars',
                    'https://user@fred.stlouisfed.org/graph/fredgraph.csv']:
            with self.assertRaisesRegex(ProbeError,'destination'):
                client.read(url)
    def test_api_key_goes_only_to_alpaca(self):
        requests=[]
        class Opener:
            def open(self,req,timeout):
                requests.append(req)
                return io.BytesIO(b'{}')
        client=SourceHttp(opener=Opener(),interval=0)
        with patch.dict(os.environ,{'ALPACA_API_KEY':'fixture-key','ALPACA_SECRET_KEY':'fixture-secret'}):
            client.read('https://fred.stlouisfed.org/graph/fredgraph.csv')
            client.read('https://data.alpaca.markets/v1beta1/options/bars')
        self.assertNotIn('Apca-api-key-id',dict(requests[0].header_items()))
        self.assertEqual(dict(requests[1].header_items())['Apca-api-key-id'],'fixture-key')
    def test_forbidden_body_is_not_echoed_or_retried(self):
        class Opener:
            def open(self,req,timeout):
                raise HTTPError(req.full_url,403,'Forbidden',{},io.BytesIO(b'sensitive-fixture'))
        client=SourceHttp(opener=Opener(),interval=0)
        with self.assertRaises(ProbeError) as caught:
            client.read('https://fred.stlouisfed.org/graph/fredgraph.csv')
        self.assertNotIn('sensitive-fixture',str(caught.exception))
        self.assertEqual(client.calls,1)
