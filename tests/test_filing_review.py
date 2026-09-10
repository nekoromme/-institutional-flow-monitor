"""原書類で確認した例外が、別の数字・時期へ広がらないことを確認する。"""

import copy
import unittest

from flow_probe.bulk13f import select_filings
from flow_probe.filing_review import apply_filing_reviews, compare_tables, object_hash, pilot_comparison
from flow_probe.holdings import aggregate_security


def example_filing():
    row = {"accession": "original", "row_id": "1", "cusip": "00848K309", "issuer": "Example",
           "class": "COM", "shares": 120, "unit": "SH", "put_call": "", "discretion": "SOLE",
           "other_managers": "", "reported_value": 600, "votes": [120, 0, 0]}
    return {"accession": "original", "cik": "0000000001", "period": "2026-03-31",
            "cover_period": "2026-03-31", "filed": "2026-05-01", "name": "Example Manager",
            "form": "13F-HR", "amendment_type": "", "amendment_number": None,
            "actual_entries": 1, "declared_entries": 2, "table_valid": False,
            "rows": [row], "reported_by": [], "included_managers": []}


def example_catalog(filing):
    return {"filings": [{"accession": filing["accession"], "bulk_filing_sha256": object_hash(filing),
                         "decision": "full_information_table_verified_header_count_warning_retained",
                         "original_declared_entries": 2, "actual_entries": 1,
                         "primary": {"source_url": "https://www.sec.gov/example-primary.xml"},
                         "information_table": {"source_url": "https://www.sec.gov/example-table.xml"},
                         "allow_table_for_analysis": True, "period": "2026-03-31", "filed": "2026-05-01",
                         "declared_value": 600, "comparison": {"all_rows_match": True,
                         "invalid_reported_value_rows": 0, "summed_reported_value": 600}}]}


def aggregate(filings, as_of="2026-06-01"):
    states = select_filings(filings, "2026-03-31", as_of)
    return aggregate_security(states, "00848K309")


class SourceReviewTests(unittest.TestCase):
    def test_review_restores_reported_rows_and_preserves_original_warning(self):
        original = example_filing()
        before = aggregate({"original": original})
        revised, applications = apply_filing_reviews({"original": original}, example_catalog(original))
        after = aggregate(revised)
        self.assertEqual(before["reported_share_sum_before_overlap_resolution"], 0)
        self.assertEqual(after["reported_share_sum_before_overlap_resolution"], 120)
        self.assertFalse(original["table_valid"])
        self.assertNotIn("reviewed_table_usable", original)
        self.assertFalse(after["source_reviewed_filings"][0]["original_table_valid"])
        self.assertEqual(applications[0]["status"], "matched")
        self.assertIsNone(after["institutional_ownership_percent"])

    def test_changed_share_count_withdraws_the_review(self):
        original = example_filing()
        catalog = example_catalog(original)
        changed = copy.deepcopy(original)
        changed["rows"][0]["shares"] = 99999
        revised, applications = apply_filing_reviews({"original": changed}, catalog)
        self.assertEqual(applications[0]["status"], "input_changed_review_not_applied")
        self.assertEqual(len(aggregate(revised)["unresolved_filings"]), 1)

    def test_changed_joint_reporting_relationship_withdraws_the_review(self):
        original = example_filing()
        catalog = example_catalog(original)
        original["reported_by"] = [{"cik": "0000000002", "name": "Another", "file_number": ""}]
        _, applications = apply_filing_reviews({"original": original}, catalog)
        self.assertEqual(applications[0]["status"], "input_changed_review_not_applied")

    def test_verification_never_makes_a_future_filing_available(self):
        original = example_filing()
        revised, _ = apply_filing_reviews({"original": original}, example_catalog(original))
        self.assertEqual(aggregate(revised, as_of="2026-05-01")["included_rows"], 0)

    def test_later_restatement_does_not_inherit_originals_exception(self):
        original = example_filing()
        later = copy.deepcopy(original)
        later.update(accession="replacement", form="13F-HR/A", amendment_type="RESTATEMENT",
                     amendment_number=1, filed="2026-05-20")
        later["rows"][0].update(accession="replacement", shares=300)
        revised, _ = apply_filing_reviews({"original": original, "replacement": later}, example_catalog(original))
        result = aggregate(revised)
        self.assertEqual(result["included_rows"], 0)
        self.assertEqual(len(result["unresolved_filings"]), 1)

    def test_a_count_exception_cannot_waive_a_period_mismatch(self):
        original = example_filing()
        original["cover_period"] = "2025-12-31"
        with self.assertRaisesRegex(ValueError, "invalid_source_review_allowance"):
            apply_filing_reviews({"original": original}, example_catalog(original))

    def test_a_failed_full_table_comparison_cannot_restore_holdings(self):
        original = example_filing()
        catalog = example_catalog(original)
        catalog["filings"][0]["comparison"]["all_rows_match"] = False
        with self.assertRaisesRegex(ValueError, "invalid_source_review_allowance"):
            apply_filing_reviews({"original": original}, catalog)

    def test_multiset_comparison_does_not_hide_a_duplicate(self):
        row = example_filing()["rows"][0]
        result = compare_tables([row, row], [row])
        self.assertFalse(result["all_rows_match"])
        self.assertEqual(result["source_only_rows"], 1)

    def test_missing_vote_value_is_not_changed_to_zero(self):
        row = example_filing()["rows"][0]
        other = {**row, "votes": [120, None, 0]}
        self.assertFalse(compare_tables([row], [other])["all_rows_match"])

    def test_comparison_does_not_use_baseline_from_a_later_information_date(self):
        result = pilot_comparison({"as_of": "2026-03-01"}, {"as_of": "2026-06-01"}, {})
        self.assertEqual(result["status"], "different_as_of_dates_no_comparison")


if __name__ == "__main__":
    unittest.main()
