"""保有率の材料を作る。未解決の合算を機関保有率として認定しない。"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from .bulk13f import PARSER_VERSION, digest, extract_archive, integer, merge_archives, select_filings
from .http_client import SafeHttp
from .holdings_reference import reviewed_denominator
from .filing_review import apply_filing_reviews, load_catalog, pilot_comparison


VERSION = "holdings-audit-0.3"
SECURITIES = {
    "UAVS": {"cik": "0000008504", "cusip": "00848K309"},
    "QMCO": {"cik": "0000709283", "cusip": "747906600"},
    "MU": {"cik": "0000723125", "cusip": "595112103"},
    "ONTO": {"cik": "0000704532", "cusip": "683344105"},
    "AAOI": {"cik": "0001158114", "cusip": "03823U102"},
}
ARCHIVES = {
    "01dec2025-28feb2026_form13f.zip": "ff340fc5dc0bc60539b03c3a97fffa3fe4ab0d728d6507a4aadabb4a888fbe01",
    "01mar2026-31may2026_form13f.zip": "05f4da8f526cd471a387af45661a8cb3bbfaf345d3ec9c2cefd31349aa257e63",
}
LISTS = {
    "2025-12-31": ("2025q4", "6a70489393bc1d1977d00fabedb58550e904fe9c0a4a98cf35361920b91847c9"),
    "2026-03-31": ("2026q1", "8c6b35079ccadbadda946d59523aadc697e5e8dc64b60ae1aae3c4c3047455fb"),
}
TAGS = {"us-gaap": "CommonStockSharesOutstanding", "dei": "EntityCommonStockSharesOutstanding"}
COMMON_CLASSES = {
    "COM", "COMSHS", "COMNEW", "COMMON", "COMMONSTOCK", "COMMONSTOCKS", "COMSTOCK",
    "COMMONSTK", "COMMSTK", "COMMONSTOCKUSD", "COMMONLARGECAP", "COMMONORDINARYSTOCK",
    "COMMONORDINARY", "DOMESTICCOMMONSTOCK", "STOCK", "CMN", "CS", "EQUITY", "EQUITIES",
    "EQTY", "ORDINARYSHARES", "LISTEDSTOCK", "SHS", "COMM", "COMSTK",
}
# 公式二期リストのCOM SHSと、同じCUSIPの記載を今回照合した表記。
REVIEWED_CLASS_VARIANTS = {"00848K309": {"INCNEWCOMSHS"}}


def row_problem(row):
    # CUSIPは公式リストで別途確認する。自由記載の株式種類も無条件には信用しない。
    if row["put_call"].strip():
        return "option"
    if row["unit"] != "SH":
        return "not_share_units"
    if row["shares"] is None:
        return "invalid_share_count"
    normalized = re.sub(r"[^A-Z0-9]", "", row["class"].upper())
    if normalized not in COMMON_CLASSES | REVIEWED_CLASS_VARIANTS.get(row["cusip"], set()):
        return "unreviewed_security_class"
    return None


def form_number(value):
    match = re.fullmatch(r"0*28-0*(\d+)", (value or "").strip())
    return "28-" + str(int(match[1])) if match else (value or "").strip()


def match_security_list(path, cusip):
    found = []
    for line in Path(path).read_text().splitlines():
        if line[:9] == cusip:
            found.append({"cusip": cusip, "issuer": line[10:40].strip(),
                          "class": line[40:67].strip(), "status": line[67:70].strip()})
    if len(found) != 1 or found[0]["status"] == "*D*" or found[0]["class"] not in {"COM", "COM SHS"}:
        raise ValueError("security_list_match_requires_review:" + cusip)
    return found[0]


def select_denominator(concepts, period, as_of):
    candidates = []
    for taxonomy, concept in concepts.items():
        for fact in concept.get("units", {}).get("shares", []):
            if (fact.get("filed", "9999") >= as_of or fact.get("end", "9999") > period or
                    fact.get("start") or fact.get("form") not in {"10-K", "10-K/A", "10-Q", "10-Q/A"}):
                continue
            shares = integer(fact.get("val"))
            if not shares:
                continue
            candidates.append({"taxonomy": taxonomy, "tag": TAGS[taxonomy], "shares": shares,
                               "end": fact["end"], "filed": fact["filed"], "accession": fact["accn"],
                               "source_url": concept["source_url"]})
    if not candidates:
        return {"status": "unavailable", "shares": None}
    # 同日があれば貸借対照表の株数を優先。なければ期末より前の最も近い日。
    exact_gaap = [f for f in candidates if f["end"] == period and f["taxonomy"] == "us-gaap"]
    pool = exact_gaap or candidates
    latest_end = max(f["end"] for f in pool)
    pool = [f for f in pool if f["end"] == latest_end]
    latest_filed = max(f["filed"] for f in pool)
    pool = [f for f in pool if f["filed"] == latest_filed]
    if len({f["shares"] for f in pool}) > 1:
        return {"status": "conflicting_latest_facts", "shares": None, "conflicting_facts": pool}
    chosen = sorted(pool, key=lambda f: (f["taxonomy"] != "us-gaap", f["accession"]))[0]
    other_values = [f for f in candidates if f["end"] == chosen["end"] and f["shares"] != chosen["shares"]]
    return {**chosen, "status": "exact_period" if chosen["end"] == period else "older_date_proxy",
            "age_days": (date.fromisoformat(period) - date.fromisoformat(chosen["end"])).days,
            "same_date_other_values": other_values,
            "security_class_verified": False}


def aggregate_security(states, cusip):
    included = defaultdict(list)
    rejected, unresolved = [], []
    confidential, confidential_unknown = [], []
    for cik, state in states.items():
        if state["issues"]:
            if cusip in state["target_cusips_in_history"]:
                unresolved.append({"cik": cik, "name": state["name"], "reasons": state["issues"],
                                   "accessions": [f["accession"] for f in state["filings"]]})
            continue
        for row in state["rows"]:
            if row["cusip"] != cusip:
                continue
            problem = row_problem(row)
            if problem:
                rejected.append({"cik": cik, "accession": row["accession"], "row_id": row["row_id"],
                                 "reason": problem, "class": row["class"], "shares": row["shares"]})
            elif row["shares"] > 0:
                included[cik].append(row)
        if included.get(cik) and state["confidential_omitted"]:
            confidential.append(cik)
        if included.get(cik) and state.get("confidential_status_unknown", False):
            confidential_unknown.append(cik)

    file_to_ciks = defaultdict(set)
    for cik, state in states.items():
        for filing in state["filings"]:
            if filing.get("file_number"):
                file_to_ciks[form_number(filing["file_number"])].add(cik)

    def resolve(ref):
        if ref.get("cik") and ref["cik"] != "0000000000":
            return ref["cik"]
        ciks = file_to_ciks.get(form_number(ref.get("file_number")), set())
        return next(iter(ciks)) if len(ciks) == 1 else None

    relationships = {}
    unknown_references = []
    duplicate_rows = []
    for cik, rows in included.items():
        state = states[cik]
        fingerprints = Counter(json.dumps({k: v for k, v in row.items() if k not in {"accession", "row_id"}},
                                          sort_keys=True) for row in rows)
        for signature, count in fingerprints.items():
            if count > 1:
                duplicate_rows.append({"cik": cik, "copies": count, "row": json.loads(signature)})
        for filing in state["filings"]:
            active_rows = [r for r in rows if r["accession"] == filing["accession"]]
            if not active_rows:
                continue
            references = [("reported_by", ref) for ref in filing["reported_by"]]
            sequences = {r["sequence"]: r for r in filing["included_managers"]}
            for row in active_rows:
                value = row["other_managers"].strip().upper()
                if value in {"", "NONE", "N/A", "NA"} or (value == "0" and "0" not in sequences):
                    continue
                parts = re.split(r"[,;\s]+", value)
                for part in parts:
                    key = str(integer(part))
                    if key not in sequences:
                        unknown_references.append({"cik": cik, "accession": filing["accession"],
                                                   "row_id": row["row_id"], "sequence": part})
                    else:
                        references.append(("included_manager", sequences[key]))
            for kind, ref in references:
                other = resolve(ref)
                if other is None:
                    unknown_references.append({"cik": cik, "accession": filing["accession"],
                                               "relationship": kind, "reference": ref})
                elif other not in states:
                    unknown_references.append({"cik": cik, "accession": filing["accession"],
                                               "relationship": kind, "reference": ref,
                                               "reason": "referenced_manager_not_in_acquired_period"})
                elif other != cik and other in included:
                    identity = (min(cik, other), max(cik, other))
                    record = relationships.setdefault(identity, {"ciks": list(identity), "kinds": set()})
                    record["kinds"].add(kind)
    manager_totals = {cik: sum(row["shares"] for row in rows) for cik, rows in included.items()}
    edges = []
    for record in relationships.values():
        first, second = record["ciks"]
        equal_positions = sorted(set(r["shares"] for r in included[first]) &
                                 set(r["shares"] for r in included[second]))
        edges.append({"ciks": record["ciks"], "names": [states[c]["name"] for c in record["ciks"]],
                      "kinds": sorted(record["kinds"]),
                      "manager_share_totals": [manager_totals[c] for c in record["ciks"]],
                      "equal_row_share_counts": equal_positions,
                      "status": "potential_overlap_not_automatically_subtracted"})
    ledger = [{"cik": cik, "name": states[cik]["name"], "shares": shares,
               "accessions": sorted({r["accession"] for r in included[cik]}),
               "rows": included[cik]} for cik, shares in sorted(manager_totals.items())]
    source_reviews = []
    for cik, rows in included.items():
        active = {r["accession"] for r in rows}
        for filing in states[cik]["filings"]:
            if filing["accession"] in active and filing.get("source_table_review"):
                source_reviews.append({"cik": cik, "name": states[cik]["name"],
                                       **filing["source_table_review"],
                                       "original_table_valid": filing["table_valid"],
                                       "reviewed_table_usable": filing.get("reviewed_table_usable", False)})
    return {
        "status": "diagnostic_sum_not_certified_ownership", "cusip": cusip,
        "reported_share_sum_before_overlap_resolution": sum(manager_totals.values()),
        "positive_reporting_managers": len(manager_totals),
        "included_rows": sum(len(r) for r in included.values()),
        "excluded_row_reasons": dict(Counter(r["reason"] for r in rejected)),
        "unreviewed_class_rows": [r for r in rejected if r["reason"] == "unreviewed_security_class"],
        "unresolved_filings": unresolved, "potential_overlap_relationships": edges,
        "unresolved_manager_references": unknown_references, "repeated_row_payloads": duplicate_rows,
        "confidential_omission_managers": confidential,
        "confidential_status_unknown_managers": confidential_unknown,
        "top_reported_positions": sorted(({k: v for k, v in m.items() if k != "rows"} for m in ledger),
                                         key=lambda m: (-m["shares"], m["cik"]))[:10],
        "ledger": ledger,
        "source_reviewed_filings": source_reviews,
        "institutional_ownership_percent": None, "low_ownership_eligible": None,
    }


def _get_file(client, path, url, expected=None, max_bytes=16_000_000):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        body = client.read(url, max_bytes=max_bytes)
        temp = path.with_suffix(path.suffix + ".part")
        temp.write_bytes(body)
        if expected and digest(temp) != expected:
            temp.unlink()
            raise ValueError("source_snapshot_changed_requires_review")
        temp.replace(path)
    if expected and digest(path) != expected:
        raise ValueError("cached_snapshot_hash_mismatch")
    return {"source_url": url, "sha256": digest(path), "bytes": path.stat().st_size}


def public_summary(report):
    """件数と代表例を公開用にまとめる。全行の監査記録は再実行で生成できる。"""
    summary = {k: v for k, v in report.items() if k != "quarters"}
    summary["detail_policy"] = "lists_are_counted_and_sampled; full_local_audit_is_reproducible"
    summary["quarters"] = {}
    sampled = ("unreviewed_class_rows", "unresolved_filings", "potential_overlap_relationships",
               "unresolved_manager_references", "repeated_row_payloads", "confidential_omission_managers",
               "amended_managers_with_target_in_history", "confidential_status_unknown_managers")
    for period, quarter in report["quarters"].items():
        public_quarter = {k: v for k, v in quarter.items() if k != "symbols"}
        public_quarter["symbols"] = {}
        for symbol, result in quarter["symbols"].items():
            item = {k: v for k, v in result.items() if k not in sampled}
            limit = 12 if symbol in {"UAVS", "QMCO"} else 3
            for key in sampled:
                item[key + "_count"] = len(result[key])
                item[key + "_sample"] = result[key][:limit]
            public_quarter["symbols"][symbol] = item
        summary["quarters"][period] = public_quarter
    return summary


def run(root, *, fetch=False, as_of="2026-06-01"):
    """今回の二期に範囲を固定。後日、同じURLが改訂されたら自動上書きしない。"""
    if not "2026-03-01" <= as_of <= "2026-06-01":
        raise ValueError("as_of_outside_acquired_filing_coverage")
    date.fromisoformat(as_of)
    client = SafeHttp(timeout=25, max_requests=30, max_seconds=900) if fetch else None
    bulk_dir = root / "data/sec-bulk"
    manifests, extracted = [], []
    for name, checksum in ARCHIVES.items():
        path = bulk_dir / name
        if not path.exists() and client is None:
            raise ValueError("missing_local_archive_use_fetch")
        url = "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/" + name
        manifests.append(_get_file(client, path, url, checksum, max_bytes=125_000_000))
        cache = path.with_suffix(path.suffix + ".extracted.json")
        result = json.loads(cache.read_text()) if cache.exists() else None
        if (not result or result.get("sha256") != checksum or
                result.get("parser_version") != PARSER_VERSION or
                result.get("target_cusips") != sorted(v["cusip"] for v in SECURITIES.values())):
            result = extract_archive(path, {v["cusip"] for v in SECURITIES.values()})
            cache.write_text(json.dumps(result))
        extracted.append(result)
    filings = merge_archives(extracted)
    review_catalog = load_catalog()
    filings, review_applications = apply_filing_reviews(filings, review_catalog)
    concepts, fact_manifests = {}, {}
    for symbol, security in SECURITIES.items():
        concepts[symbol] = {}
        for taxonomy, tag in TAGS.items():
            path = root / "data/sec-facts" / f"{symbol}-{taxonomy}.json"
            if not path.exists() and client is None:
                raise ValueError("missing_local_facts_use_fetch")
            url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{security['cik']}/{taxonomy}/{tag}.json"
            fact_manifests[f"{symbol}/{taxonomy}"] = _get_file(client, path, url)
            concept = json.loads(path.read_text())
            if str(concept.get("cik")).zfill(10) != security["cik"] or concept.get("tag") != tag:
                raise ValueError("company_concept_identity_mismatch")
            concept["source_url"] = url
            concepts[symbol][taxonomy] = concept
    quarters, ledgers, list_manifests = {}, {}, {}
    for period, (quarter, checksum) in LISTS.items():
        if period >= as_of:
            continue
        path = bulk_dir / f"13flist{quarter}.txt"
        if not path.exists() and client is None:
            raise ValueError("missing_local_security_list_use_fetch")
        list_manifests[period] = _get_file(client, path, f"https://www.sec.gov/files/investment/13flist{quarter}.txt", checksum)
        states = select_filings(filings, period, as_of)
        results = {}
        for symbol, security in SECURITIES.items():
            official = match_security_list(path, security["cusip"])
            result = aggregate_security(states, security["cusip"])
            ledgers[f"{period}/{symbol}"] = result.pop("ledger")
            result["official_security"] = official
            denominator = reviewed_denominator(symbol, select_denominator(concepts[symbol], period, as_of))
            result["denominator"] = denominator
            result["diagnostic_sum_divided_by_denominator_percent"] = (
                round(100 * result["reported_share_sum_before_overlap_resolution"] / denominator["shares"], 6)
                if denominator.get("shares") else None)
            history_rows = [r for f in filings.values() if f["period"] == period and f["filed"] < as_of
                            for r in f["rows"] if r["cusip"] == security["cusip"] and row_problem(r) is None]
            result["unprocessed_all_versions_share_sum"] = sum(r["shares"] for r in history_rows)
            result["amended_managers_with_target_in_history"] = [
                {"cik": c, "name": s["name"], "operations": s["operations"], "issues": s["issues"]}
                for c, s in states.items() if security["cusip"] in s["target_cusips_in_history"] and
                any(o["operation"] != "original" for o in s["operations"])]
            results[symbol] = result
        quarters[period] = {
            "reporting_managers_in_acquired_files": len(states),
            "unresolved_manager_count_all_securities": sum(bool(s["issues"]) for s in states.values()),
            "source_filings": sum(len(s["operations"]) for s in states.values()),
            "symbols": results,
        }
    report = {"version": VERSION, "generated_at": datetime.now(timezone.utc).isoformat(),
              "as_of": as_of, "date_policy": "filing_date_strictly_before_as_of; no_acceptance_time_in_bulk",
              "holdings_filing_coverage": {"from": "2025-12-01", "through": "2026-05-31"},
              "current_ownership": False, "backtest_labels_ready": False,
              "total_information_rows_scanned": sum(a["information_rows"] for a in extracted),
              "unique_filings_in_archives": len(filings), "archives": manifests,
              "security_lists": list_manifests, "company_concept_cache_files": fact_manifests,
              "source_review_catalog_version": review_catalog["version"],
              "source_review_catalog_matches": review_applications,
              "limitations": ["13F_does_not_cover_all_institutions_or_all_positions",
                              "absence_is_not_zero", "joint_reporting_not_fully_resolved",
                              "bulk_release_time_not_used_as_original_filing_time",
                              "denominator_share_class_needs_filing_review",
                              "no_low_ownership_classification_or_return_backtest"],
              "quarters": quarters}
    baseline_path = Path(__file__).resolve().parents[1] / "docs/evidence/sec-bulk-holdings-2026-09-10.json"
    baseline = json.loads(baseline_path.read_text())
    report["pilot_review_comparison"] = pilot_comparison(report, baseline, review_catalog)
    out = root / "diagnostics/holdings"
    out.mkdir(parents=True, exist_ok=True)
    (out / "manager-ledger.json").write_text(json.dumps(ledgers, ensure_ascii=False, indent=2) + "\n")
    report["manager_ledger_sha256"] = digest(out / "manager-ledger.json")
    (out / "bulk-audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    (out / "bulk-summary.json").write_text(json.dumps(public_summary(report), ensure_ascii=False, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description="SEC二期の公開保有資料を集計・点検する")
    parser.add_argument("--fetch", action="store_true", help="不足する公式公開ファイルをGETで取得")
    parser.add_argument("--as-of", default="2026-06-01", help="この日より前の提出情報だけを使用")
    args = parser.parse_args()
    report = run(Path.cwd(), fetch=args.fetch, as_of=args.as_of)
    print(json.dumps({"version": VERSION, "as_of": report["as_of"],
                      "information_rows": report["total_information_rows_scanned"],
                      "backtest_labels_ready": report["backtest_labels_ready"],
                      "report": "diagnostics/holdings/bulk-audit.json",
                      "summary": "diagnostics/holdings/bulk-summary.json"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
