import unittest
from datetime import date, timedelta

from flow_probe.alpaca import split_adjustment_audit
from flow_probe.research import prepare_daily
from flow_probe.sec import compare_sample_filings, match_common_share_candidates


class ResearchTests(unittest.TestCase):
    def fixture(self):
        start = date(2024, 6, 1)
        days = [(start+timedelta(days=i)).isoformat() for i in range(100)
                if (start+timedelta(days=i)).weekday() < 5]
        sessions = [{"date": d, "open": "09:30", "close": "16:00"} for d in days]
        bars = [{"t": d+"T04:00:00Z", "v": 1000} for d in days]
        return sessions, bars

    def test_history_excludes_today_and_future_split(self):
        sessions, bars = self.fixture()
        rows, _ = prepare_daily({"QMCO": bars}, sessions, {"QMCO": {"status": "comparable_sample"}})
        before = next(r for r in rows if r["date"] == "2024-08-26")
        after = next(r for r in rows if r["date"] == "2024-08-27")
        self.assertEqual(before["baseline_volumes_in_decision_day_shares"], [1000]*60)
        self.assertEqual(after["baseline_volumes_in_decision_day_shares"], [50]*60)
        bars[-1]["v"] = 9999999
        later, _ = prepare_daily({"QMCO": bars}, sessions, {"QMCO": {"status": "comparable_sample"}})
        self.assertEqual(rows[:-1], later[:-1])
        self.assertNotIn(9999999, later[-1]["baseline_volumes_in_decision_day_shares"])

    def test_missing_day_does_not_shift_history_or_become_zero(self):
        sessions, bars = self.fixture()
        rows, _ = prepare_daily({"QMCO": bars[1:]}, sessions, {"QMCO": {"status": "comparable_sample"}})
        self.assertEqual(rows[60]["status"], "missing_or_duplicate_day")
        self.assertNotIn("baseline_volumes_in_decision_day_shares", rows[60])

    def test_bad_adjustment_blocks_only_windows_containing_that_day(self):
        sessions, bars = self.fixture()
        audit = {"QMCO": {"status": "needs_review", "coverage_complete": True,
                          "unresolved_days": [sessions[0]["date"]]}}
        rows, _ = prepare_daily({"QMCO": bars}, sessions, audit)
        self.assertEqual(rows[60]["status"], "unresolved_adjustment_in_current_or_history")
        self.assertEqual(rows[61]["status"], "prepared")

    def test_documented_factor_is_not_inferred_from_rounded_close(self):
        raw = {"t": "2024-08-26T04:00:00Z", "o": 0.5, "h": 0.6, "l": 0.4, "c": 0.5001, "v": 1000}
        adj = {**raw, "o": 10, "h": 12, "l": 8, "c": 10.001, "v": 50}
        q = split_adjustment_audit({"QMCO": [raw]}, {"QMCO": [adj]}, complete=True, through="2024-08-28")["QMCO"]
        self.assertEqual(q["status"], "comparable_sample")
        self.assertEqual(q["provider_adjustment_segments"][0]["price_multiplier"], 20)
        self.assertEqual(q["pairs_with_price_differences_inside_rounding_tolerance"], 1)
        adj["c"] = 12
        q = split_adjustment_audit({"QMCO": [raw]}, {"QMCO": [adj]}, complete=True, through="2024-08-28")["QMCO"]
        self.assertEqual(q["status"], "needs_review")

    def test_share_class_variant_and_legal_suffix_do_not_change_identity(self):
        entries = [{"issuer": "QUANTUM CORP", "class": "COM", "cusip": "747906600", "shares": 100,
                    "unit": "SH", "put_call": ""},
                   {"issuer": "AGEAGLE AERIAL SYSTEMS INC", "class": "COM SHS", "cusip": "00848K309",
                    "shares": 200, "unit": "SH", "put_call": ""}]
        out = match_common_share_candidates(entries, {"QMCO": {"title": "QUANTUM CORP /DE/"},
                                                     "UAVS": {"title": "AgEagle Aerial Systems Inc."}})
        self.assertEqual(out["QMCO"]["reported_share_sum"], 100)
        self.assertEqual(out["UAVS"]["reported_share_sum"], 200)

    def test_missing_or_changed_security_never_implies_zero_holding(self):
        a = {"status": "ok", "form": "13F-HR", "manager_cik": "1", "confidential_omitted": "false",
             "primary_period_matches": True, "declared_entry_count_matches": True,
             "period_of_report": "2026-03-31", "candidate_matches": {}}
        b = {**a, "period_of_report": "2026-06-30",
             "candidate_matches": {"UAVS": {"cusip": "00848K309", "reported_share_sum": 100}}}
        q = compare_sample_filings([a, b], ("UAVS",))["by_symbol"]["UAVS"]
        self.assertEqual(q["status"], "unknown")
        self.assertNotIn("reported_share_change", q)
        a["candidate_matches"] = {"UAVS": {"cusip": "00848K200", "reported_share_sum": 50}}
        q = compare_sample_filings([a, b], ("UAVS",))["by_symbol"]["UAVS"]
        self.assertEqual(q["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
