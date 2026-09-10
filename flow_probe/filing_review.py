"""原提出書類との照合と、今回確認できた例外の適用。

表紙の件数が違うことと、取得した保有行が欠けていることは別の問題。
例外は提出番号・元のデータ全体の指紋が一致する書類にだけ適用する。
同じ運用会社の別の四半期へ、確認済み判定を引き継がない。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from zipfile import ZipFile

from .bulk13f import _table, digest, integer
from .http_client import SafeHttp
from .sec import field, local_name

CATALOG_PATH = "docs/evidence/filing-source-review-2026-09-10.json"
CATALOG_VERSION = "filing-source-review-0.1"
ROW_FIELDS = ("cusip", "issuer", "class", "shares", "unit", "put_call", "discretion",
              "other_managers", "reported_value", "votes")


def object_hash(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def canonical_row(row):
    result = {k: row[k] for k in ROW_FIELDS}
    # 表記の大小だけを統一する。株数や銘柄名を近似で一致させない。
    for key in ("cusip", "unit", "put_call", "discretion"):
        result[key] = result[key].strip().upper()
    return result


def xml_rows(body):
    root = ET.fromstring(body)
    rows = []
    for node in root.iter():
        if local_name(node.tag).lower() != "infotable":
            continue
        rows.append(canonical_row({
            "cusip": field(node, "cusip"), "issuer": field(node, "nameOfIssuer"),
            "class": field(node, "titleOfClass"), "shares": integer(field(node, "sshPrnamt")),
            "unit": field(node, "sshPrnamtType"), "put_call": field(node, "putCall"),
            "discretion": field(node, "investmentDiscretion"),
            "other_managers": field(node, "otherManager"), "reported_value": integer(field(node, "value")),
            "votes": [integer(field(node, k)) for k in ("Sole", "Shared", "None")],
        }))
    return rows


def tsv_row(row):
    return canonical_row({
        "cusip": row["CUSIP"], "issuer": row["NAMEOFISSUER"], "class": row["TITLEOFCLASS"],
        "shares": integer(row["SSHPRNAMT"]), "unit": row["SSHPRNAMTTYPE"], "put_call": row["PUTCALL"],
        "discretion": row["INVESTMENTDISCRETION"], "other_managers": row["OTHERMANAGER"],
        "reported_value": integer(row["VALUE"]),
        "votes": [integer(row[k]) for k in ("VOTING_AUTH_SOLE", "VOTING_AUTH_SHARED", "VOTING_AUTH_NONE")],
    })


def compare_tables(original_rows, bulk_rows):
    # 集合にすると、同じ行が2回入った誤りを見逃す。出現回数も比較する。
    original = Counter(object_hash(canonical_row(r)) for r in original_rows)
    bulk = Counter(object_hash(canonical_row(r)) for r in bulk_rows)
    return {"original_rows": len(original_rows), "bulk_rows": len(bulk_rows),
            "all_rows_match": original == bulk,
            "source_only_rows": sum((original - bulk).values()),
            "bulk_only_rows": sum((bulk - original).values()),
            "duplicate_payload_excess": sum(n - 1 for n in original.values()),
            "unique_cusips": len({r["cusip"] for r in original_rows}),
            "summed_reported_value": sum(r["reported_value"] for r in original_rows
                                         if r["reported_value"] is not None),
            "invalid_reported_value_rows": sum(r["reported_value"] is None for r in original_rows)}


def load_catalog(root=None):
    root = root or Path(__file__).resolve().parents[1]
    catalog = json.loads((root / CATALOG_PATH).read_text())
    if catalog.get("version") != CATALOG_VERSION:
        raise ValueError("unsupported_filing_review_catalog")
    return catalog


def apply_filing_reviews(filings, catalog):
    """一括ファイルの原記録を残し、確認済みの書類だけに別の利用許可を付ける。"""
    result = dict(filings)
    applications = []
    for case in catalog["filings"]:
        accession = case["accession"]
        current = filings.get(accession)
        if current is None:
            applications.append({"accession": accession, "status": "not_in_input"})
            continue
        if object_hash(current) != case["bulk_filing_sha256"]:
            # 元の数字、日付、株式種類、共同報告の相手が変わった場合もここで保留。
            applications.append({"accession": accession, "status": "input_changed_review_not_applied"})
            continue
        case_summary = {k: case[k] for k in ("accession", "decision", "original_declared_entries",
                                           "actual_entries", "primary", "information_table")}
        case_summary["whole_ownership_verified"] = False
        updated = {**current, "source_table_review": case_summary}
        if case["allow_table_for_analysis"]:
            expected_date = case["period"]
            if (current["period"] != expected_date or current.get("cover_period") != expected_date or
                    current["filed"] != case["filed"] or current["filed"] < expected_date or
                    not case["comparison"]["all_rows_match"] or
                    case["comparison"]["invalid_reported_value_rows"] or
                    case["comparison"]["summed_reported_value"] != case["declared_value"]):
                raise ValueError("invalid_source_review_allowance")
            # table_validは元の判定のまま残す。表紙の誤りを「なかったこと」にしない。
            updated["reviewed_table_usable"] = True
        result[accession] = updated
        applications.append({"accession": accession, "status": "matched",
                             "allow_table_for_analysis": case["allow_table_for_analysis"]})
    return result, applications


def pilot_comparison(report, baseline, catalog):
    """修正前後を示す。保有の増減を機関の売買の正解ラベルへ変換しない。"""
    if report["as_of"] != baseline["as_of"]:
        return {"status": "different_as_of_dates_no_comparison"}
    periods = ("2025-12-31", "2026-03-31")
    if any(p not in report["quarters"] or p not in baseline["quarters"] for p in periods):
        return {"status": "required_period_missing"}
    output = {"status": "diagnostic_comparison_only", "as_of": report["as_of"],
              "baseline_version": baseline["version"], "current_version": report["version"],
              "institutional_flow_label": None, "symbols": {}}
    for symbol in ("UAVS", "QMCO", "MU", "ONTO", "AAOI"):
        changes = {}
        for period in periods:
            old = baseline["quarters"][period]["symbols"][symbol]
            new = report["quarters"][period]["symbols"][symbol]
            before = old["reported_share_sum_before_overlap_resolution"]
            after = new["reported_share_sum_before_overlap_resolution"]
            changes[period] = {"before_review": before, "after_review": after, "difference": after - before}
        output["symbols"][symbol] = {
            "by_period": changes,
            "change_in_observed_reported_sum": changes[periods[1]]["after_review"] - changes[periods[0]]["after_review"],
            "net_institutional_buying_confirmed": False,
        }
    current = report["quarters"][periods[1]]["symbols"]["UAVS"]
    previous = report["quarters"][periods[0]]["symbols"]["UAVS"]
    overlap = catalog["overlap"]
    reviewed_accessions = {r["accession"] for r in current.get("source_reviewed_filings", [])}
    same_position = next((r for r in current["potential_overlap_relationships"]
                          if r["ciks"] == overlap["ciks"] and
                          r["manager_share_totals"] == [overlap["identical_target_shares_each"]] * 2), None)
    if same_position and set(overlap["accessions"]).issubset(reviewed_accessions):
        before = previous["reported_share_sum_before_overlap_resolution"]
        upper = current["reported_share_sum_before_overlap_resolution"]
        lower = upper - overlap["identical_target_shares_each"]
        sensitivity = {
            "scope": "only_the_identical_calamos_position; all_other_recorded_sums_held_fixed",
            "is_confidence_interval": False, "covers_all_data_uncertainty": False,
            "reported_sum_if_full_overlap": lower, "reported_sum_if_no_overlap": upper,
            "change_if_full_overlap": lower - before, "change_if_no_overlap": upper - before,
            "other_unresolved_manager_references": len(current["unresolved_manager_references"]),
        }
        # 両期末の分母を原書類で確認できた場合だけ、株数の増加と割合の変化を並べる。
        denoms = [q["denominator"] for q in (previous, current)]
        if all(d.get("status") == "exact_period" and d.get("security_class_verified") for d in denoms):
            d0, d1 = (d["shares"] for d in denoms)
            sensitivity.update({
                "shares_outstanding_change_percent": round(100 * (d1 / d0 - 1), 6),
                "previous_diagnostic_ratio_percent": round(100 * before / d0, 6),
                "current_diagnostic_ratio_percent_if_full_overlap": round(100 * lower / d1, 6),
                "current_diagnostic_ratio_percent_if_no_overlap": round(100 * upper / d1, 6),
                "diagnostic_ratio_change_percentage_points_if_full_overlap": round(100 * lower / d1 - 100 * before / d0, 6),
                "diagnostic_ratio_change_percentage_points_if_no_overlap": round(100 * upper / d1 - 100 * before / d0, 6),
            })
        output["symbols"]["UAVS"]["single_issue_sensitivity"] = sensitivity
    else:
        output["symbols"]["UAVS"]["single_issue_sensitivity"] = {"status": "reviewed_case_not_active_or_changed"}
    return output


def verify_sources(root, *, fetch=False):
    """原表全行と一括ファイルを再照合する。研究上の判断自体は自動変更しない。"""
    catalog = load_catalog(root)
    client = SafeHttp(timeout=25, max_requests=20, max_seconds=600) if fetch else None
    originals, primaries = {}, {}
    for case in catalog["filings"]:
        accession = case["accession"]
        for key in ("primary", "information_table"):
            source = case[key]
            name = Path(urlparse(source["source_url"]).path).name
            path = root / "diagnostics/filing-review" / accession / name
            if not path.exists():
                if client is None:
                    raise ValueError("missing_original_document_use_fetch")
                body = client.read(source["source_url"], max_bytes=18_000_000)
                if hashlib.sha256(body).hexdigest() != source["sha256"]:
                    raise ValueError("source_changed_requires_new_review")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
            body = path.read_bytes()
            if hashlib.sha256(body).hexdigest() != source["sha256"]:
                raise ValueError("cached_original_hash_mismatch")
            if key == "primary":
                primaries[accession] = ET.fromstring(body)
            else:
                originals[accession] = xml_rows(body)
    bulk = {a: [] for a in originals}
    for source in catalog["archives"]:
        name = Path(urlparse(source["source_url"]).path).name
        path = root / "data/sec-bulk" / name
        if not path.exists():
            raise ValueError("missing_bulk_archive_run_holdings_fetch_first")
        if digest(path) != source["sha256"]:
            raise ValueError("bulk_archive_changed_requires_new_review")
        with ZipFile(path) as archive:
            for row in _table(archive, "INFOTABLE", ("ACCESSION_NUMBER", "CUSIP")):
                if row["ACCESSION_NUMBER"] in bulk:
                    bulk[row["ACCESSION_NUMBER"]].append(tsv_row(row))
    results = []
    for case in catalog["filings"]:
        accession = case["accession"]
        compared = compare_tables(originals[accession], bulk[accession])
        primary = primaries[accession]
        primary_matches = (
            integer(field(primary, "tableEntryTotal")) == case["original_declared_entries"] and
            integer(field(primary, "tableValueTotal")) == case["declared_value"] and
            datetime.strptime(field(primary, "periodOfReport"), "%m-%d-%Y").date().isoformat() == case["period"] and
            field(primary, "additionalInformation") == case["additional_information"])
        reproduced = compared == case["comparison"] and primary_matches
        results.append({"accession": accession, "comparison": compared,
                        "recorded_review_reproduced": reproduced})
        if not reproduced:
            raise ValueError("source_review_did_not_reproduce")
    parent = Counter(object_hash(r) for r in originals[catalog["overlap"]["accessions"][0]])
    child = Counter(object_hash(r) for r in originals[catalog["overlap"]["accessions"][1]])
    common_count = sum((parent & child).values())
    if common_count != catalog["overlap"]["identical_position_rows"]:
        raise ValueError("overlap_review_did_not_reproduce")
    output = {"version": CATALOG_VERSION, "verified_at": datetime.now(timezone.utc).isoformat(),
              "all_recorded_reviews_reproduced": True, "filings": results,
              "identical_calamos_position_rows": common_count,
              "economic_overlap_confirmed": False}
    (root / "diagnostics/filing-review/verification.json").write_text(json.dumps(output, indent=2) + "\n")
    return output


def main():
    parser = argparse.ArgumentParser(description="確認済みの原報告4件を再照合する")
    parser.add_argument("--fetch", action="store_true", help="不足する原報告を公式公開経路から取得")
    args = parser.parse_args()
    result = verify_sources(Path.cwd(), fetch=args.fetch)
    print(json.dumps({"original_filings": len(result["filings"]),
                      "all_recorded_reviews_reproduced": result["all_recorded_reviews_reproduced"]}))


if __name__ == "__main__":
    main()
