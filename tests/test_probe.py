"""試験の成績が偽の『成功』にならないための、合成データによる確認。"""

import unittest
from datetime import datetime, timedelta, timezone

from flow_probe.__main__ import assert_no_secrets
from flow_probe.alpaca import (NY, completed_sessions, fetch_pages, missing_minute_samples, quality,
                               session_times, timestamp_ns)
from flow_probe.http_client import ProbeError, SafeHttp, error_category
from flow_probe.sec import (match_common_share_candidates, parse_information_table,
                            select_original_filings)


class FakeClient:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def metrics(self):
        return {"received_bytes": len(self.requests)*100,
                "http_requests": len(self.requests), "retries": 0}

    def json(self, url, params):
        self.requests.append((url, dict(params)))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply


class MarketTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2025, 1, 6, 9, 30, tzinfo=NY)
        self.end = self.start + timedelta(minutes=2)

    def test_short_first_page_still_fetches_later_symbols(self):
        client = FakeClient([
            {"bars": {"AAPL": [{"t": "first"}]}, "next_page_token": "next"},
            {"bars": {"MU": [{"t": "second"}]}, "next_page_token": None},
        ])
        rows, meta = fetch_pages(client, "bars", ("AAPL", "MU"), self.start, self.end,
                                 feed="sip", timeframe="1Min", limit=1000)
        self.assertTrue(meta["complete"])
        self.assertEqual(len(rows["MU"]), 1)
        self.assertEqual(client.requests[1][1]["page_token"], "next")
        self.assertTrue(all(p["feed"] == "sip" for _, p in client.requests))

    def test_page_cap_is_incomplete_even_with_some_data(self):
        client = FakeClient([{"bars": {"AAPL": []}, "next_page_token": "next"}])
        _, meta = fetch_pages(client, "bars", ("AAPL",), self.start, self.end,
                              feed="sip", timeframe="1Min", max_pages=1)
        self.assertFalse(meta["complete"])
        self.assertEqual(meta["status"], "partial_page_limit")

    def test_repeated_token_stops_instead_of_double_counting_forever(self):
        client = FakeClient([{"bars": {}, "next_page_token": "same"}]*2)
        _, meta = fetch_pages(client, "bars", ("AAPL",), self.start, self.end,
                              feed="sip", timeframe="1Min")
        self.assertFalse(meta["complete"])
        self.assertEqual(meta["error"]["category"], "repeated_page_token")

    def test_forbidden_does_not_silently_fall_back_to_iex(self):
        client = FakeClient([ProbeError("subscription_restriction", 403)])
        _, meta = fetch_pages(client, "bars", ("AAPL",), self.start, self.end,
                              feed="sip", timeframe="1Min")
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(meta["http_status"], 403)
        self.assertFalse(meta["complete"])

    def test_nanoseconds_are_not_collapsed_to_microseconds(self):
        a = timestamp_ns("2025-01-06T14:30:00.123456001Z")
        b = timestamp_ns("2025-01-06T14:30:00.123456999Z")
        self.assertEqual(b-a, 998)

    def test_market_session_needs_delay_margin(self):
        rows = [{"date": "2025-07-02", "open": "09:30", "close": "16:00"},
                {"date": "2025-07-03", "open": "09:30", "close": "13:00"}]
        now = datetime(2025, 7, 3, 13, 20, tzinfo=NY)
        self.assertEqual(len(completed_sessions(rows, now)), 1)
        self.assertEqual(len(completed_sessions(rows, now+timedelta(minutes=11))), 2)
        a, b = session_times(rows[1])
        self.assertEqual((b-a).total_seconds()/60, 210)

    def test_missing_bars_are_not_assumed_zero_volume(self):
        sessions = [{"date": "2025-01-06", "open": "09:30", "close": "16:00"}]
        q = quality({"AAPL": []}, "bars", sessions, timeframe="1Min", complete=True,
                    start=self.start, end=self.end)["AAPL"]
        self.assertEqual(q["missing_regular_minutes"], 390)
        q_partial = quality({"AAPL": []}, "bars", sessions, timeframe="1Min", complete=False,
                            start=self.start, end=self.end)["AAPL"]
        self.assertIsNone(q_partial["missing_regular_minutes"])

    def test_invalid_bar_and_duplicate_are_detected(self):
        row = {"t": "2025-01-06T14:30:00Z", "o": 10, "h": 11, "l": 9, "c": 10, "v": 2}
        bad = {**row, "h": 8}
        q = quality({"AAPL": [row, row, bad]}, "bars", [], timeframe="1Min", complete=True,
                    start=self.start, end=self.end)["AAPL"]
        self.assertEqual(q["invalid_records"], 1)
        self.assertEqual(q["identical_rows_or_duplicate_bar_times"], 1)

    def test_missing_minute_audit_stays_inside_real_session(self):
        session = {"date": "2025-01-06", "open": "09:30", "close": "09:33"}
        rows = [{"t": "2025-01-06T14:31:00Z"}]
        samples = missing_minute_samples(rows, [session])
        self.assertEqual([r.minute for r in samples], [30, 32])


class SecurityTests(unittest.TestCase):
    def test_secrets_suppress_output(self):
        with self.assertRaises(ProbeError):
            assert_no_secrets("accidental fake_private_value exposure", ["fake_private_value"])
        assert_no_secrets("counts and timings", ["fake_private_value"])

    def test_error_does_not_echo_provider_body(self):
        category = error_category(403, b"potentially private arbitrary body")
        self.assertNotIn("private", category)
        self.assertEqual(error_category(422, b"subscription does not permit querying recent SIP data"),
                         "subscription_restriction")

    def test_credentials_are_never_sent_to_sec(self):
        class Reply:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, amount): return b"{}"
        class Opener:
            def open(self, request, timeout):
                self.request = request
                return Reply()
        opener = Opener()
        client = SafeHttp("fake_key", "fake_secret", opener=opener, interval=0)
        client.json("https://www.sec.gov/files/company_tickers.json")
        headers = str(opener.request.headers)
        self.assertNotIn("fake_key", headers)
        self.assertNotIn("fake_secret", headers)

    def test_trading_and_unrelated_hosts_are_blocked(self):
        client = SafeHttp(interval=0)
        for url in ["https://paper-api.alpaca.markets/v2/orders",
                    "https://paper-api.alpaca.markets/v2/account", "https://example.com/"]:
            with self.assertRaises(ProbeError):
                client.json(url)
        self.assertEqual(client.calls, 0)


class FilingsTests(unittest.TestCase):
    def test_options_and_principal_amount_are_excluded_from_share_candidates(self):
        body = b'''<informationTable xmlns="urn:sample">
        <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass>
        <cusip>037833100</cusip><shrsOrPrnAmt><sshPrnamt>100</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
        <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass>
        <cusip>037833100</cusip><putCall>CALL</putCall><shrsOrPrnAmt><sshPrnamt>999</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
        </informationTable>'''
        rows = parse_information_table(body)
        matched = match_common_share_candidates(rows, {"AAPL": {"title": "Apple Inc."}})
        self.assertEqual(matched["AAPL"]["reported_share_sum"], 100)
        self.assertFalse(matched["AAPL"]["verified_universal_identifier_mapping"])

    def test_absent_name_match_is_not_zero_institutional_ownership(self):
        result = match_common_share_candidates([], {"AAPL": {"title": "Apple Inc."}})
        self.assertEqual(result["AAPL"]["status"], "not_matched_not_zero_ownership")
        self.assertNotIn("reported_share_sum", result["AAPL"])

    def test_future_filings_and_amendments_do_not_replace_original(self):
        recent = {
            "form": ["13F-HR", "13F-HR/A", "13F-HR", "13F-HR"],
            "filingDate": ["2025-11-14", "2025-08-20", "2025-08-14", "2025-05-14"],
            "reportDate": ["2025-09-30", "2025-06-30", "2025-06-30", "2025-03-31"],
            "accessionNumber": [f"0000000001-25-00000{i}" for i in range(4)],
            "primaryDocument": ["primary_doc.xml"]*4,
        }
        rows = select_original_filings(recent, "2025-09-10")
        self.assertEqual([r["period_of_report"] for r in rows], ["2025-06-30", "2025-03-31"])
        self.assertTrue(all(r["form"] == "13F-HR" for r in rows))

    def test_sec_display_path_resolves_only_known_transform_directory(self):
        recent = {"form": ["13F-HR"], "filingDate": ["2025-08-14"],
                  "reportDate": ["2025-06-30"], "accessionNumber": ["0000000001-25-000001"],
                  "primaryDocument": ["xslForm13F_X02/primary_doc.xml"]}
        rows = select_original_filings(recent, "2025-09-10")
        self.assertEqual(rows[0]["primary_document"], "primary_doc.xml")
        recent["primaryDocument"] = ["../primary_doc.xml"]
        with self.assertRaises(ProbeError):
            select_original_filings(recent, "2025-09-10")


if __name__ == "__main__":
    unittest.main()
