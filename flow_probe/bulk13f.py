"""SEC公式一括データを読む。原報告と訂正を足し合わせない。"""

from __future__ import annotations

import csv
import hashlib
import io
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zipfile import ZipFile


PARSER_VERSION = "bulk13f-0.1.1"


def integer(value):
    try:
        number = Decimal(str(value))
        if number.is_finite() and number >= 0 and number == number.to_integral_value():
            return int(number)
    except InvalidOperation:
        pass
    return None


def sec_date(value):
    return datetime.strptime(value, "%d-%b-%Y").date().isoformat()


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _table(archive, name, required):
    info = archive.getinfo(name + ".tsv")
    if info.file_size > 1_500_000_000:
        raise ValueError("bulk_member_too_large")
    with archive.open(info) as raw:
        reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"), delimiter="\t")
        if not set(required).issubset(reader.fieldnames or []):
            raise ValueError("bulk_schema_changed:" + name)
        for row in reader:
            if None in row:
                raise ValueError("bulk_invalid_row:" + name)
            yield row


def extract_archive(path, cusips):
    """情報表を逐次走査。指定CUSIP以外の大量の行はメモリへ保持しない。"""
    filings = {}
    total_rows = 0
    csv.field_size_limit(4_000_000)
    with ZipFile(path) as archive:
        for row in _table(archive, "SUBMISSION", (
                "ACCESSION_NUMBER", "FILING_DATE", "SUBMISSIONTYPE", "CIK", "PERIODOFREPORT")):
            accession = row["ACCESSION_NUMBER"]
            if accession in filings:
                raise ValueError("duplicate_submission")
            filings[accession] = {
                "accession": accession, "cik": row["CIK"].zfill(10),
                "filed": sec_date(row["FILING_DATE"]),
                "period": sec_date(row["PERIODOFREPORT"]),
                "form": row["SUBMISSIONTYPE"], "rows": [], "actual_entries": 0,
                "reported_by": [], "included_managers": [],
            }
        for row in _table(archive, "COVERPAGE", (
                "ACCESSION_NUMBER", "AMENDMENTNO", "AMENDMENTTYPE", "REPORTTYPE",
                "FILINGMANAGER_NAME", "FORM13FFILENUMBER", "REPORTCALENDARORQUARTER")):
            filing = filings[row["ACCESSION_NUMBER"]]
            filing.update({
                "name": row["FILINGMANAGER_NAME"], "file_number": row["FORM13FFILENUMBER"],
                "report_type": row["REPORTTYPE"], "amendment_type": row["AMENDMENTTYPE"],
                "amendment_number": integer(row["AMENDMENTNO"]),
                "cover_period": sec_date(row["REPORTCALENDARORQUARTER"]),
            })
        for row in _table(archive, "SUMMARYPAGE", (
                "ACCESSION_NUMBER", "TABLEENTRYTOTAL", "ISCONFIDENTIALOMITTED")):
            filings[row["ACCESSION_NUMBER"]].update({
                "declared_entries": integer(row["TABLEENTRYTOTAL"]),
                "confidential_omitted": row["ISCONFIDENTIALOMITTED"] != "N",
            })
        for name, destination in (("OTHERMANAGER", "reported_by"),
                                  ("OTHERMANAGER2", "included_managers")):
            for row in _table(archive, name, ("ACCESSION_NUMBER", "CIK", "FORM13FFILENUMBER", "NAME")):
                filings[row["ACCESSION_NUMBER"]][destination].append({
                    "cik": row["CIK"].zfill(10) if row["CIK"] else None,
                    "file_number": row["FORM13FFILENUMBER"], "name": row["NAME"],
                    "sequence": str(integer(row.get("SEQUENCENUMBER", ""))),
                })
        seen = set()
        fields = ("ACCESSION_NUMBER", "INFOTABLE_SK", "CUSIP", "NAMEOFISSUER", "TITLEOFCLASS",
                  "SSHPRNAMT", "SSHPRNAMTTYPE", "PUTCALL", "OTHERMANAGER", "INVESTMENTDISCRETION",
                  "VALUE", "VOTING_AUTH_SOLE", "VOTING_AUTH_SHARED", "VOTING_AUTH_NONE")
        for row in _table(archive, "INFOTABLE", fields):
            total_rows += 1
            filing = filings[row["ACCESSION_NUMBER"]]
            filing["actual_entries"] += 1
            if row["CUSIP"].strip() not in cusips:
                continue
            identity = (row["ACCESSION_NUMBER"], row["INFOTABLE_SK"])
            if identity in seen:
                raise ValueError("duplicate_information_row_id")
            seen.add(identity)
            filing["rows"].append({
                "accession": identity[0], "row_id": identity[1], "cusip": row["CUSIP"].strip(),
                "issuer": row["NAMEOFISSUER"], "class": row["TITLEOFCLASS"],
                "shares": integer(row["SSHPRNAMT"]), "unit": row["SSHPRNAMTTYPE"],
                "put_call": row["PUTCALL"], "discretion": row["INVESTMENTDISCRETION"],
                "other_managers": row["OTHERMANAGER"], "reported_value": integer(row["VALUE"]),
                "votes": [integer(row[k]) for k in fields[-3:]],
            })
    for filing in filings.values():
        notice = filing["form"] in {"13F-NT", "13F-NT/A"}
        filing["table_valid"] = (
            filing.get("cover_period") == filing["period"] and filing["filed"] >= filing["period"] and
            ((notice and filing["actual_entries"] == 0) or
             (not notice and filing.get("declared_entries") == filing["actual_entries"]))
        )
    return {"parser_version": PARSER_VERSION, "archive_name": Path(path).name,
            "sha256": digest(path), "bytes": Path(path).stat().st_size,
            "information_rows": total_rows, "target_cusips": sorted(cusips), "filings": filings}


def merge_archives(archives):
    result = {}
    for archive in archives:
        for accession, filing in archive["filings"].items():
            if accession in result and result[accession] != filing:
                raise ValueError("conflicting_accession_across_archives")
            result[accession] = filing
    return result


def select_filings(filings, period, as_of):
    """当日提出分は使わない。RESTATEMENTは全置換、NEW HOLDINGSは追加。"""
    grouped = defaultdict(list)
    for filing in filings.values():
        if filing["period"] == period and filing["filed"] < as_of:
            grouped[filing["cik"]].append(filing)
    states = {}
    for cik, history in grouped.items():
        history.sort(key=lambda f: (f["filed"], f.get("amendment_number") or 0, f["accession"]))
        selected, issues, operations = [], [], []
        previous_number = 0
        amendment_numbers = Counter(f["amendment_number"] for f in history if f["form"].endswith("/A"))
        for filing in history:
            amendment = filing["form"].endswith("/A")
            kind = filing.get("amendment_type", "")
            if not amendment:
                if selected:
                    issues.append("multiple_originals")
                selected = [filing]
                operation = "original"
            elif kind == "RESTATEMENT":
                # 全表を再提出する訂正なので、それ以前の追加分も含めて置き換える。
                selected = [filing]
                issues = []
                operation = "replace"
            elif kind == "NEW HOLDINGS":
                if not selected:
                    issues.append("addition_without_base")
                if filing.get("amendment_number") != previous_number + 1:
                    issues.append("missing_or_out_of_order_amendment")
                selected.append(filing)
                operation = "append"
            else:
                issues.append("unknown_amendment_type")
                operation = "unresolved"
            if amendment and (filing.get("amendment_number") is None or
                              amendment_numbers[filing["amendment_number"]] > 1):
                issues.append("ambiguous_amendment_order")
            if not filing["table_valid"] and not filing.get("reviewed_table_usable", False):
                issues.append("entry_count_or_period_mismatch")
            previous_number = filing.get("amendment_number") or 0
            operations.append({"accession": filing["accession"], "filed": filing["filed"],
                               "operation": operation, "amendment_number": filing.get("amendment_number")})
        # 後の全訂正で修復された元報告の不備は、現行表の不備として引き継がない。
        rows = [row for filing in selected for row in filing["rows"]]
        states[cik] = {"cik": cik, "name": history[-1].get("name", ""),
                       "filings": selected, "rows": rows, "operations": operations,
                       "issues": sorted(set(issues)),
                       "target_cusips_in_history": sorted({r["cusip"] for f in history for r in f["rows"]}),
                       "confidential_omitted": any(f.get("confidential_omitted", False) for f in selected)}
    return states
