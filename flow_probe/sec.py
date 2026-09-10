"""公的な保有資料を取得し、銘柄照合に必要な情報と不足を確かめる。"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET

from .alpaca import SYMBOLS
from .http_client import ProbeError, SafeHttp

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
# Berkshire Hathaway。保有率が低い銘柄の抽出ではなく、資料構造の試験用。
MANAGER_CIK = "0001067983"


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def field(element, name: str) -> str:
    for child in element.iter():
        if local_name(child.tag).lower() == name.lower():
            return (child.text or "").strip()
    return ""


def parse_information_table(body: bytes) -> list:
    """名前空間の違いに対応。株数とオプションを混ぜて合計しない。"""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise ProbeError("invalid_filing_xml") from None
    entries = []
    for node in root.iter():
        if local_name(node.tag).lower() != "infotable":
            continue
        amount = field(node, "sshPrnamt").replace(",", "")
        if not re.fullmatch(r"\d+", amount):
            raise ProbeError("invalid_reported_share_amount")
        entries.append({"issuer": field(node, "nameOfIssuer"),
                        "class": field(node, "titleOfClass"),
                        "cusip": field(node, "cusip").upper(),
                        "shares": int(amount),
                        "unit": field(node, "sshPrnamtType").upper(),
                        "put_call": field(node, "putCall").upper()})
    return entries


def select_original_filings(recent: dict, as_of_date: str) -> list:
    """最新二つの対象四半期の当初報告を選ぶ。訂正を黙って上書きしない。"""
    selected = []
    for i, form in enumerate(recent.get("form", [])):
        if form != "13F-HR":
            continue
        try:
            filing_date = recent["filingDate"][i]
            report_date = recent["reportDate"][i]
            accession = recent["accessionNumber"][i]
            primary = recent["primaryDocument"][i]
        except (KeyError, IndexError):
            raise ProbeError("incomplete_submission_metadata") from None
        if filing_date > as_of_date or not report_date or report_date > as_of_date:
            continue
        if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
            raise ProbeError("invalid_accession")
        # SECの履歴には、表示用の変換フォルダが付いたパスも載る。
        # その既知の一段だけを除き、元のXMLを取得する。任意の経路は許可しない。
        original_primary = primary
        if re.fullmatch(r"xslForm13F_[A-Za-z0-9]+/[A-Za-z0-9_.-]+", primary):
            primary = primary.split("/", 1)[1]
        if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", primary):
            raise ProbeError("invalid_primary_document_name")
        accepted = recent.get("acceptanceDateTime", [])
        selected.append({"form": form, "filing_date": filing_date,
                         "period_of_report": report_date, "accession": accession,
                         "primary_document": primary,
                         "primary_document_as_listed": original_primary,
                         "accepted_at": accepted[i] if i < len(accepted) else None})
    selected.sort(key=lambda row: (row["period_of_report"], row["filing_date"]), reverse=True)
    unique = []
    for row in selected:
        if row["period_of_report"] not in {r["period_of_report"] for r in unique}:
            unique.append(row)
    return unique[:2]


def normalize_name(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def match_common_share_candidates(entries: list, issuers: dict) -> dict:
    """会社名が完全一致する普通株だけを照合候補にする。推測で確定しない。"""
    result = {}
    for symbol, issuer in issuers.items():
        matches = [r for r in entries
                   if normalize_name(r["issuer"]) == normalize_name(issuer["title"])
                   and r["unit"] == "SH" and not r["put_call"]
                   and r["class"].upper().strip() in {"COM", "COMMON STOCK"}]
        cusips = {r["cusip"] for r in matches}
        if len(cusips) == 1 and all(re.fullmatch(r"[A-Z0-9]{9}", c) for c in cusips):
            result[symbol] = {"status": "candidate_name_and_common_class_only",
                              "cusip": next(iter(cusips)), "matched_rows": len(matches),
                              "reported_share_sum": sum(r["shares"] for r in matches),
                              "verified_universal_identifier_mapping": False}
        elif len(cusips) > 1:
            result[symbol] = {"status": "ambiguous_multiple_securities"}
        else:
            # 保有0ではない。保有なし・省略・表記揺れを区別できない段階。
            result[symbol] = {"status": "not_matched_not_zero_ownership"}
    return result


def fetch_filing(client: SafeHttp, metadata: dict, issuers: dict) -> dict:
    base = ("https://www.sec.gov/Archives/edgar/data/" + str(int(MANAGER_CIK)) + "/"
            + metadata["accession"].replace("-", "") + "/")
    index = client.json(base + "index.json")
    if not isinstance(index, dict):
        raise ProbeError("unexpected_filing_index")
    names = [r.get("name", "") for r in index.get("directory", {}).get("item", [])]
    xml_names = [n for n in names if n.lower().endswith(".xml")
                 and n != metadata["primary_document"] and re.fullmatch(r"[A-Za-z0-9_.-]+", n)]
    primary_body = client.read(base + metadata["primary_document"])
    try:
        primary = ET.fromstring(primary_body)
    except ET.ParseError:
        raise ProbeError("invalid_primary_filing_xml") from None
    for name in xml_names[:4]:
        body = client.read(base + name)
        entries = parse_information_table(body)
        if not entries:
            continue
        return {**metadata, "status": "ok", "information_table_url": base+name,
                "primary_document_url": base+metadata["primary_document"],
                "information_table_sha256": hashlib.sha256(body).hexdigest(),
                "information_table_bytes": len(body), "table_rows": len(entries),
                "is_amendment": field(primary, "isAmendment"),
                "confidential_omitted": field(primary, "isConfidentialOmitted"),
                "candidate_matches": match_common_share_candidates(entries, issuers)}
    raise ProbeError("information_table_not_found_in_bounded_search")


def run_sec(client: SafeHttp, as_of_date: str) -> dict:
    report = {"status": "pending", "issuer_lookup": {}, "filings": [],
              "manager_cik": MANAGER_CIK,
              "scope": "two_original_filings_one_manager; not_total_institutional_ownership",
              "unresolved": [
                  "CUSIP_to_ticker_history_needs_authoritative_mapping",
                  "all_managers_and_amendments_not_yet_aggregated",
                  "share_class_and_point_in_time_denominator_require_validation",
                  "missing_holding_is_not_zero_ownership",
              ]}
    try:
        table = client.json(TICKERS_URL)
        if not isinstance(table, dict):
            raise ProbeError("unexpected_ticker_schema")
        issuers = {r["ticker"]: r for r in table.values() if r.get("ticker") in SYMBOLS}
        report["issuer_lookup"] = {s: {"cik": r["cik_str"], "title": r["title"]}
                                   for s, r in issuers.items()}
        report["issuer_lookup_source"] = TICKERS_URL
        report["ticker_file_contains_cusip"] = any("cusip" in r for r in table.values())
    except ProbeError as exc:
        report.update(status="blocked", error=exc.summary())
        return report
    try:
        submissions = client.json("https://data.sec.gov/submissions/CIK" + MANAGER_CIK + ".json")
        recent = submissions.get("filings", {}).get("recent", {})
        filings = select_original_filings(recent, as_of_date)
        report["original_filings_selected"] = len(filings)
        report["amendment_count_in_recent_history"] = recent.get("form", []).count("13F-HR/A")
        if len(filings) < 2:
            report["unresolved"].append("older_submission_history_files_need_followup")
    except ProbeError as exc:
        # 同じ接続先への失敗を銘柄数分繰り返さず、原因を明示して止める。
        report.update(status="partial", submissions_error=exc.summary())
        return report
    for metadata in filings:
        try:
            report["filings"].append(fetch_filing(client, metadata, issuers))
        except ProbeError as exc:
            report["filings"].append({**metadata, "status": "error", "error": exc.summary()})
    report["denominator_probe"] = {}
    for symbol, issuer in issuers.items():
        url = ("https://data.sec.gov/api/xbrl/companyconcept/CIK" + str(issuer["cik_str"]).zfill(10)
               + "/dei/EntityCommonStockSharesOutstanding.json")
        try:
            facts = client.json(url)
            values = [r for r in facts.get("units", {}).get("shares", [])
                      if r.get("filed", "9999") <= as_of_date and r.get("end", "9999") <= as_of_date]
            dates = sorted({r["end"] for r in values})
            report["denominator_probe"][symbol] = {
                "status": "available_unvalidated" if values else "no_usable_records",
                "records": len(values), "oldest_period": dates[0] if dates else None,
                "latest_period": dates[-1] if dates else None,
                "source": url, "point_in_time_share_class_reconciliation_complete": False,
            }
        except ProbeError as exc:
            report["denominator_probe"][symbol] = {"status": "error", "error": exc.summary()}
    report["status"] = "sample_access_ok" if len(report["filings"]) == 2 and all(
        r["status"] == "ok" for r in report["filings"]) else "partial"
    return report
