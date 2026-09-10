"""保有比率を過大評価したり、未来の情報を混ぜたりする誤りの検査。"""

import copy
import csv
import io
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from flow_probe.bulk13f import extract_archive, merge_archives, select_filings
from flow_probe.holdings import aggregate_security, match_security_list, select_denominator
from flow_probe.holdings_reference import reviewed_denominator


CUSIP = "00848K309"
PERIOD = "2026-03-31"
AS_OF = "2026-06-01"


def row(shares=100, **changes):
    result = {"accession": "a", "row_id": "1", "cusip": CUSIP, "issuer": "Example",
              "class": "COM", "shares": shares, "unit": "SH", "put_call": "",
              "discretion": "SOLE", "other_managers": "", "reported_value": shares * 5,
              "votes": [shares, 0, 0]}
    result.update(changes)
    return result


def filing(accession="a", rows=None, **changes):
    result = {"accession": accession, "cik": "0000000001", "filed": "2026-05-01",
              "period": PERIOD, "form": "13F-HR", "name": "Manager A", "file_number": "028-00100",
              "amendment_type": "", "amendment_number": None, "table_valid": True,
              "reported_by": [], "included_managers": [], "rows": [row()] if rows is None else rows,
              "confidential_omitted": False}
    result.update(changes)
    result["rows"] = [{**r, "accession": accession} for r in result["rows"]]
    return result


def states(*filings, as_of=AS_OF):
    return select_filings({f["accession"]: f for f in filings}, PERIOD, as_of)


def concept(taxonomy, facts):
    return {taxonomy: {"source_url": "https://data.sec.gov/example", "units": {"shares": facts}}}


def fact(shares=1000, **changes):
    result = {"val": shares, "end": PERIOD, "filed": "2026-05-15", "form": "10-Q", "accn": "x"}
    result.update(changes)
    return result


class HoldingsTests(unittest.TestCase):
    def test_restatement_replaces_then_new_holdings_adds(self):
        original = filing(rows=[row(100)])
        restated = filing("b", [row(80)], form="13F-HR/A", amendment_type="RESTATEMENT",
                          amendment_number=1, filed="2026-05-15")
        added = filing("c", [row(5)], form="13F-HR/A", amendment_type="NEW HOLDINGS",
                       amendment_number=2, filed="2026-05-15")
        result = aggregate_security(states(original, restated, added), CUSIP)
        self.assertEqual(result["reported_share_sum_before_overlap_resolution"], 85)
        self.assertEqual(result["positive_reporting_managers"], 1)

    def test_future_and_same_day_corrections_are_not_inputs(self):
        original = filing(rows=[row(100)])
        future = filing("b", [row(500)], form="13F-HR/A", amendment_type="RESTATEMENT",
                        amendment_number=1, filed=AS_OF)
        self.assertEqual(aggregate_security(states(original, future), CUSIP)
                         ["reported_share_sum_before_overlap_resolution"], 100)

    def test_restatement_can_remove_target_without_zero_ownership_claim(self):
        corrected = filing("b", [], form="13F-HR/A", amendment_type="RESTATEMENT",
                           amendment_number=1, filed="2026-05-20")
        result = aggregate_security(states(filing(), corrected), CUSIP)
        self.assertEqual(result["included_rows"], 0)
        self.assertIsNone(result["institutional_ownership_percent"])
        self.assertIsNone(result["low_ownership_eligible"])

    def test_addition_without_base_and_count_mismatch_are_not_zero(self):
        addition = filing(form="13F-HR/A", amendment_type="NEW HOLDINGS", amendment_number=1)
        broken = filing("b", cik="0000000002", table_valid=False)
        result = aggregate_security(states(addition, broken), CUSIP)
        self.assertEqual(len(result["unresolved_filings"]), 2)
        self.assertEqual(result["positive_reporting_managers"], 0)
        self.assertIsNone(result["low_ownership_eligible"])

    def test_full_restatement_can_repair_missing_or_broken_original(self):
        repaired = filing("b", [row(70)], form="13F-HR/A", amendment_type="RESTATEMENT",
                          amendment_number=1, filed="2026-05-20")
        for originals in ([], [filing(table_valid=False)]):
            result = aggregate_security(states(*originals, repaired), CUSIP)
            self.assertEqual(result["reported_share_sum_before_overlap_resolution"], 70)

    def test_missing_intermediate_addition_keeps_manager_unresolved(self):
        addition = filing("b", [row(5)], form="13F-HR/A", amendment_type="NEW HOLDINGS",
                          amendment_number=2, filed="2026-05-20")
        result = aggregate_security(states(filing(), addition), CUSIP)
        self.assertEqual(result["unresolved_filings"][0]["reasons"], ["missing_or_out_of_order_amendment"])

    def test_notice_does_not_add_the_referenced_managers_shares_again(self):
        notice = filing("b", [], cik="0000000002", form="13F-NT",
                        reported_by=[{"cik": "0000000001", "name": "Manager A", "file_number": ""}])
        result = aggregate_security(states(filing(), notice), CUSIP)
        self.assertEqual(result["reported_share_sum_before_overlap_resolution"], 100)
        self.assertEqual(result["positive_reporting_managers"], 1)

    def test_shared_discretion_rows_are_disjoint_parts_not_blanket_duplicates(self):
        rows = [row(80, discretion="DFND"), row(20, discretion="DFND", other_managers="1", row_id="2")]
        parent = filing(rows=rows, included_managers=[{"cik": "0000000002", "sequence": "1", "file_number": "", "name": "B"}])
        notice = filing("b", [], cik="0000000002", form="13F-NT")
        result = aggregate_security(states(parent, notice), CUSIP)
        self.assertEqual(result["reported_share_sum_before_overlap_resolution"], 100)
        self.assertEqual(result["potential_overlap_relationships"], [])

    def test_related_equal_positions_flagged_without_unproven_subtraction(self):
        one = filing(reported_by=[{"cik": "0000000002", "name": "B", "file_number": ""}])
        two = filing("b", cik="0000000002", name="Manager B")
        result = aggregate_security(states(one, two), CUSIP)
        self.assertEqual(result["reported_share_sum_before_overlap_resolution"], 200)
        self.assertEqual(result["potential_overlap_relationships"][0]["equal_row_share_counts"], [100])
        self.assertIsNone(result["institutional_ownership_percent"])

    def test_file_number_padding_resolves_same_manager(self):
        one = filing(reported_by=[{"cik": None, "name": "B", "file_number": "28-539"}])
        two = filing("b", cik="0000000002", file_number="028-00539")
        result = aggregate_security(states(one, two), CUSIP)
        self.assertEqual(len(result["potential_overlap_relationships"]), 1)
        self.assertEqual(result["unresolved_manager_references"], [])

    def test_options_and_wrong_classes_do_not_become_common_shares(self):
        rows = [row(100), row(200, put_call="Call"), row(300, unit="PRN"),
                row(400, **{"class": "PUT"}), row(500, **{"class": "Class A"})]
        result = aggregate_security(states(filing(rows=rows)), CUSIP)
        self.assertEqual(result["reported_share_sum_before_overlap_resolution"], 100)
        self.assertEqual(result["excluded_row_reasons"]["unreviewed_security_class"], 2)

    def test_repeated_payload_is_recorded_not_silently_deleted(self):
        result = aggregate_security(states(filing(rows=[row(), row(row_id="2")])), CUSIP)
        self.assertEqual(result["repeated_row_payloads"][0]["copies"], 2)
        self.assertIsNone(result["institutional_ownership_percent"])

    def test_future_denominator_does_not_leak(self):
        facts = concept("us-gaap", [fact(1000, end="2025-12-31", filed="2026-02-17"),
                                    fact(1400, filed="2026-06-25")])
        result = select_denominator(facts, PERIOD, AS_OF)
        self.assertEqual(result["shares"], 1000)
        self.assertEqual(result["status"], "older_date_proxy")

    def test_denominator_uses_fact_date_not_quarter_frame(self):
        facts = concept("dei", [fact(1200, end="2026-02-12", filed="2026-02-17"),
                               fact(1500, end="2026-05-01", filed="2026-05-05")])
        result = select_denominator(facts, PERIOD, AS_OF)
        self.assertEqual(result["shares"], 1200)
        self.assertEqual(result["age_days"], 47)

    def test_conflicting_latest_denominators_are_unknown(self):
        result = select_denominator(concept("us-gaap", [fact(1000), fact(1100)]), PERIOD, AS_OF)
        self.assertIsNone(result["shares"])
        self.assertEqual(result["status"], "conflicting_latest_facts")

    def test_manual_share_class_review_is_not_reused_for_changed_fact(self):
        selected = {"end": "2026-02-12", "shares": 14638029, "taxonomy": "dei",
                    "accession": "0001628280-26-008558", "filed": "2026-02-17",
                    "security_class_verified": False}
        self.assertTrue(reviewed_denominator("QMCO", selected)["security_class_verified"])
        self.assertFalse(reviewed_denominator("QMCO", {**selected, "shares": 999})["security_class_verified"])

    def test_conflicting_accession_across_archives_is_an_error(self):
        first = {"filings": {"a": filing()}}
        second = copy.deepcopy(first)
        second["filings"]["a"]["rows"][0]["shares"] = 999
        with self.assertRaisesRegex(ValueError, "conflicting_accession"):
            merge_archives([first, second])

    def test_official_list_preserves_leading_zero_and_checks_class(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "list.txt"
            path.write_text(f"{CUSIP} {'Example':30}{'COM SHS':27}   \n")
            self.assertEqual(match_security_list(path, CUSIP)["cusip"], CUSIP)
            path.write_text(f"{CUSIP} {'Example':30}{'CALL':27}   \n")
            with self.assertRaises(ValueError):
                match_security_list(path, CUSIP)

    def test_zip_parser_counts_non_target_rows_and_rejects_wrong_total(self):
        tables = {
            "SUBMISSION": (["ACCESSION_NUMBER", "FILING_DATE", "SUBMISSIONTYPE", "CIK", "PERIODOFREPORT"],
                           [["a", "15-MAY-2026", "13F-HR", "1", "31-MAR-2026"]]),
            "COVERPAGE": (["ACCESSION_NUMBER", "AMENDMENTNO", "AMENDMENTTYPE", "REPORTTYPE", "FILINGMANAGER_NAME",
                           "FORM13FFILENUMBER", "REPORTCALENDARORQUARTER"],
                          [["a", "", "", "13F HOLDINGS REPORT", "A", "028-1", "31-MAR-2026"]]),
            "SUMMARYPAGE": (["ACCESSION_NUMBER", "TABLEENTRYTOTAL", "ISCONFIDENTIALOMITTED"], [["a", "3", "N"]]),
            "OTHERMANAGER": (["ACCESSION_NUMBER", "CIK", "FORM13FFILENUMBER", "NAME"], []),
            "OTHERMANAGER2": (["ACCESSION_NUMBER", "CIK", "FORM13FFILENUMBER", "NAME"], []),
            "INFOTABLE": (["ACCESSION_NUMBER", "INFOTABLE_SK", "CUSIP", "NAMEOFISSUER", "TITLEOFCLASS", "SSHPRNAMT",
                           "SSHPRNAMTTYPE", "PUTCALL", "OTHERMANAGER", "INVESTMENTDISCRETION", "VALUE",
                           "VOTING_AUTH_SOLE", "VOTING_AUTH_SHARED", "VOTING_AUTH_NONE"],
                          [["a", "1", CUSIP, "Example", "COM", "100", "SH", "", "", "SOLE", "500", "100", "0", "0"],
                           ["a", "2", "999999999", "Other", "COM", "100", "SH", "", "", "SOLE", "500", "100", "0", "0"]]),
        }
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixture.zip"
            with ZipFile(path, "w") as archive:
                for name, (fields, rows) in tables.items():
                    text = io.StringIO()
                    writer = csv.writer(text, delimiter="\t")
                    writer.writerow(fields)
                    writer.writerows(rows)
                    archive.writestr(name + ".tsv", text.getvalue())
            result = extract_archive(path, {CUSIP})
            self.assertEqual(result["information_rows"], 2)
            self.assertEqual(len(result["filings"]["a"]["rows"]), 1)
            self.assertFalse(result["filings"]["a"]["table_valid"])


if __name__ == "__main__":
    unittest.main()
