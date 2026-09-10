"""実データ取得から既存の判定処理へ渡す境界を、通信なしで検査する。"""
import copy
from datetime import date, datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from flow_probe.alpaca import NY, iso
from flow_probe.cjmb_pilot import COMPARISON, run, summarize_trial, validate_calendar
from flow_probe.http_client import ProbeError


class MarketFixture:
    def __init__(self, *, mismatched=False, missing=False):
        self.calls = 0
        days = []
        day = date(2025, 9, 1)
        while day <= date(2026, 3, 31):
            if day.weekday() < 5:
                days.append(day.isoformat())
            day += timedelta(days=1)
        self.sessions = [{'date': d, 'open': '09:30', 'close': '16:00'} for d in days]
        self.rows = [{'t': iso(datetime.fromisoformat(d+'T00:00:00').replace(tzinfo=NY)),
                      'o': 5, 'h': 6, 'l': 4, 'c': 5, 'v': 100 if d != '2026-02-02' else 10000}
                     for d in days if not (missing and d == '2026-01-05')]
        self.mismatched = mismatched

    def metrics(self):
        return {'received_bytes': 0, 'http_requests': self.calls, 'retries': 0}

    def json(self, url, params):
        self.calls += 1
        if url.endswith('/calendar'):
            return self.sessions
        assert params['symbols'] == 'CJMB' and params['feed'] == 'sip' and params['asof'] == '-'
        assert params['end'].startswith('2026-04-01')  # 米国3月末の終わりは世界標準時で翌日。
        rows = copy.deepcopy(self.rows)
        if self.mismatched and params['adjustment'] == 'split':
            rows[-1]['v'] *= 2
        return {'bars': {'CJMB': rows}, 'next_page_token': None}


class CjmbPilotTests(unittest.TestCase):
    def execute(self, client):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / COMPARISON; path.parent.mkdir(parents=True)
            path.write_bytes((Path(__file__).resolve().parents[1] / COMPARISON).read_bytes())
            return run(root, client)

    def test_full_path_uses_existing_model_and_does_not_publish_market_values(self):
        result = self.execute(MarketFixture())
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['trial']['abnormal_days'], 1)
        self.assertIsNone(result['trial']['accuracy'])
        text = json.dumps(result)
        for key in ['"raw_volume"', '"threshold_volume"', '"baseline_volumes_in_decision_day_shares"', '"volume_multiple"']:
            self.assertNotIn(key, text)

    def test_adjustment_disagreement_blocks_scoring(self):
        result = self.execute(MarketFixture(mismatched=True))
        self.assertEqual(result['status'], 'data_or_adjustment_review_required')
        self.assertNotIn('trial', result)

    def test_missing_session_is_unknown_not_silent_zero(self):
        result = self.execute(MarketFixture(missing=True))
        self.assertGreater(result['trial']['unknown_abnormal_days'], 0)
        day = next(r for r in result['trial']['daily_judgments_without_market_values'] if r['date'] == '2026-01-05')
        self.assertIsNone(day['abnormal'])

    def test_many_alerts_still_link_to_one_unscored_quarter(self):
        comparison = json.loads((Path(__file__).resolve().parents[1] / COMPARISON).read_text())
        scored = [{'symbol': 'CJMB', 'date': '2026-01-'+d, 'abnormal': True, 'repeated': True,
                   'reason': None, 'repeat_reason': None} for d in ['02', '05', '06']]
        result = summarize_trial(scored, comparison)
        self.assertEqual(result['repeated_abnormal_days'], 3)
        self.assertEqual(result['comparison_link']['linked_symbol_quarters'], 1)
        self.assertEqual(result['comparison_link']['accuracy_eligible_symbol_quarters'], 0)
        self.assertIsNone(result['institutional_flow_label'])

    def test_calendar_rejects_duplicate_or_future_session(self):
        row = {'date': '2026-01-02', 'open': '09:30', 'close': '16:00'}
        for sessions in [[row, row], [{**row, 'date': '2026-04-01'}]]:
            with self.assertRaises(ProbeError):
                validate_calendar(sessions)


if __name__ == '__main__':
    unittest.main()
