"""市場データの権限と品質を、少量の実データから確かめる。"""

from __future__ import annotations

import json
import math
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .http_client import ProbeError, SafeHttp

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
DATA_URL = "https://data.alpaca.markets/v2/stocks/"
# 取得品質の試験用。低機関保有銘柄の選定結果ではない。
SYMBOLS = ("UAVS", "MU", "ONTO", "AAOI", "QMCO")


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def timestamp_ns(value: str) -> int:
    """約定・気配の9桁の小数秒を切り捨てず比較する。"""
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)", value)
    if not match:
        raise ValueError("invalid_timestamp")
    second = datetime.fromisoformat(match[1] + match[3].replace("Z", "+00:00"))
    return int(second.timestamp()) * 1_000_000_000 + int((match[2] or "").ljust(9, "0"))


def session_times(row: dict) -> tuple[datetime, datetime]:
    return tuple(datetime.fromisoformat(row["date"] + "T" + row[field]).replace(tzinfo=NY)
                 for field in ("open", "close"))


def completed_sessions(rows: list, now: datetime) -> list:
    cutoff = now - timedelta(minutes=30)
    return sorted((r for r in rows if session_times(r)[1] <= cutoff), key=lambda r: r["date"])


def fetch_pages(client: SafeHttp, kind: str, symbols: tuple, start: datetime,
                end: datetime, *, feed: str, timeframe: str | None = None,
                limit: int = 1000, max_pages: int = 30,
                adjustment: str = "raw") -> tuple[dict, dict]:
    """全ページを取得する。上限・失敗は『取り切った』と表示しない。"""
    params = {"symbols": ",".join(symbols), "start": iso(start), "end": iso(end),
              "feed": feed, "limit": limit, "sort": "asc", "asof": "-"}
    if kind == "bars":
        params.update(timeframe=timeframe, adjustment=adjustment)
    rows = {s: [] for s in symbols}
    seen_tokens = set()
    began = time.monotonic()
    before = client.metrics()
    meta = {"feed_requested": feed, "automatic_feed_fallback": False,
            "feed_provenance": "explicit_request_parameter; provider_does_not_echo_feed",
            "kind": kind, "timeframe": timeframe, "start": iso(start), "end": iso(end),
            "symbols": list(symbols), "adjustment": adjustment if kind == "bars" else None,
            "symbol_mapping_asof": "-", "pages": 0, "complete": False,
            "http_status": None, "status": "pending"}
    try:
        for _ in range(max_pages):
            payload = client.json(DATA_URL + kind, params)
            meta["pages"] += 1
            meta["http_status"] = 200
            if not isinstance(payload, dict) or kind not in payload:
                raise ProbeError("unexpected_market_schema")
            groups = payload[kind] or {}
            if not isinstance(groups, dict) or any(s not in symbols for s in groups):
                raise ProbeError("unexpected_symbol_or_schema")
            for symbol, data in groups.items():
                if data is not None and not isinstance(data, list):
                    raise ProbeError("unexpected_market_schema")
                rows[symbol].extend(data or [])
            token = payload.get("next_page_token")
            if not token:
                meta.update(complete=True, status="ok")
                break
            if token in seen_tokens:
                raise ProbeError("repeated_page_token")
            seen_tokens.add(token)
            params["page_token"] = token
        else:
            meta["status"] = "partial_page_limit"
    except ProbeError as exc:
        meta.update(status="error", error=exc.summary())
        if exc.status is not None:
            meta["http_status"] = exc.status
    after = client.metrics()
    meta.update(records=sum(len(v) for v in rows.values()),
                elapsed_seconds=round(time.monotonic() - began, 3),
                received_bytes=after["received_bytes"] - before["received_bytes"],
                http_requests=after["http_requests"] - before["http_requests"],
                retries=after["retries"] - before["retries"])
    return rows, meta


def numeric(value) -> bool:
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def split_adjustment_audit(raw: dict, adjusted: dict, *, complete: bool) -> dict:
    """同じ日の補正前後を比較。価格や出来高の原数値は公開しない。"""
    result = {}
    for symbol, rows in raw.items():
        original = {r["t"]: r for r in rows}
        revised = {r["t"]: r for r in adjusted.get(symbol, [])}
        shared = sorted(original.keys() & revised.keys())
        invalid = mismatches = 0
        segments = []
        for stamp in shared:
            a, b = original[stamp], revised[stamp]
            if not all(numeric(r.get(k)) and r[k] > 0 for r in (a, b) for k in ("o", "h", "l", "c")):
                invalid += 1
                continue
            factor = b["c"] / a["c"]
            if not all(math.isclose(b[k], a[k]*factor, rel_tol=1e-5, abs_tol=1e-5)
                       for k in ("o", "h", "l")):
                mismatches += 1
            if not all(numeric(r.get("v")) and r["v"] >= 0 for r in (a, b)):
                invalid += 1
                continue
            # 出来高が整数に丸められる場合の1株以内の差は許容する。
            if not math.isclose(b["v"], a["v"]/factor, rel_tol=1e-6, abs_tol=1.0):
                mismatches += 1
            day = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(NY).date().isoformat()
            if not segments or not math.isclose(segments[-1]["price_multiplier"], factor, rel_tol=1e-5):
                segments.append({"first_day": day, "last_day": day,
                                 "price_multiplier": round(factor, 8), "records": 1})
            else:
                segments[-1]["last_day"] = day
                segments[-1]["records"] += 1
        result[symbol] = {
            "status": "comparable_sample" if complete and shared and not invalid and not mismatches
                and original.keys() == revised.keys() and len(rows) == len(original)
                and len(adjusted.get(symbol, [])) == len(revised) else "needs_review",
            "paired_days": len(shared), "unpaired_days": len(original.keys() ^ revised.keys()),
            "invalid_pairs": invalid, "price_volume_adjustment_mismatches": mismatches,
            "provider_adjustment_segments": segments,
            "interpretation": "observed_provider_factors; corporate_action_dates_and_identifiers_not_verified; retrieval_time_adjustment_not_point_in_time",
        }
    return result


def missing_minute_samples(rows: list, sessions: list, limit: int = 2) -> list:
    """存在しない分足を少数だけ選び、約定の実在を追加確認するための時刻を返す。"""
    present = {timestamp_ns(r["t"]) for r in rows}
    absent = []
    for session in sessions:
        current, closed = session_times(session)
        while current < closed:
            if timestamp_ns(iso(current)) not in present:
                absent.append(current)
            current += timedelta(minutes=1)
    if len(absent) <= limit:
        return absent
    # 最初の一か所だけに偏らないよう、離れた二つを標本にする。
    return [absent[0], absent[-1]][:limit]


def quality(rows: dict, kind: str, sessions: list, *, timeframe: str | None,
            complete: bool, start: datetime, end: datetime) -> dict:
    """生の価格・約定は返さず、欠損や形式不整合の件数だけ返す。"""
    windows = {r["date"]: session_times(r) for r in sessions}
    expected_minutes = sum(int((b-a).total_seconds()/60) for a, b in windows.values())
    result = {}
    for symbol, data in rows.items():
        invalid = outside = out_of_order = crossed = locked = 0
        duplicates = 0
        seen, bar_minutes, bar_days = set(), set(), set()
        previous = None
        timestamps = []
        required = {"bars": ("o", "h", "l", "c", "v"),
                    "trades": ("p", "s"), "quotes": ("bp", "bs", "ap", "as")}[kind]
        for row in data:
            try:
                ns = timestamp_ns(row["t"])
                dt = datetime.fromisoformat(row["t"].replace("Z", "+00:00"))
                if not all(numeric(row.get(k)) for k in required):
                    raise ValueError("invalid_number")
                if kind == "bars" and not (0 < row["l"] <= min(row["o"], row["c"])
                                           <= max(row["o"], row["c"]) <= row["h"] and row["v"] >= 0):
                    raise ValueError("invalid_bar")
                if kind == "trades" and (row["p"] <= 0 or row["s"] <= 0):
                    raise ValueError("invalid_trade")
                if kind == "quotes":
                    if any(row[k] < 0 for k in required):
                        raise ValueError("invalid_quote")
                    crossed += row["bp"] > row["ap"] > 0
                    locked += row["bp"] == row["ap"] > 0
                identity = row["t"] if kind == "bars" else json.dumps(row, sort_keys=True)
                duplicates += identity in seen
                seen.add(identity)
                if previous is not None and ns < previous:
                    out_of_order += 1
                previous = ns
                timestamps.append((ns, row["t"]))
                if not timestamp_ns(iso(start)) <= ns <= timestamp_ns(iso(end)):
                    outside += 1
                local_day = dt.astimezone(NY).date().isoformat()
                if kind == "bars" and timeframe == "1Day":
                    bar_days.add(local_day)
                if kind == "bars" and timeframe == "1Min" and local_day in windows:
                    a, b = windows[local_day]
                    if a <= dt < b:
                        bar_minutes.add(row["t"])
            except (ValueError, TypeError, KeyError, OverflowError):
                invalid += 1
        item = {"records": len(data), "invalid_records": invalid,
                "identical_rows_or_duplicate_bar_times": duplicates,
                "out_of_order": out_of_order, "outside_requested_interval": outside,
                "first_timestamp": min(timestamps)[1] if timestamps else None,
                "last_timestamp": max(timestamps)[1] if timestamps else None}
        if kind == "bars" and timeframe == "1Min":
            item.update(expected_regular_minutes=expected_minutes,
                        observed_regular_minutes=len(bar_minutes),
                        missing_regular_minutes=(expected_minutes-len(bar_minutes)) if complete else None,
                        missing_interpretation="absent_bar_is_not_proof_of_data_loss; do_not_fill_zero",
                        extended_or_other_records=len(data)-len(bar_minutes)-invalid)
        if kind == "bars" and timeframe == "1Day":
            item.update(expected_sessions=len(windows), observed_sessions=len(bar_days & windows.keys()),
                        missing_sessions=sorted(windows.keys()-bar_days) if complete else None,
                        volume_scope="provider_daily_bar; not_assumed_regular_hours_only")
        if kind == "quotes":
            item.update(crossed_quotes=crossed, locked_quotes=locked,
                        note="quote_anomalies_are_diagnostics_not_institution_identity")
        result[symbol] = item
    return result


def run_market(client: SafeHttp, now: datetime) -> dict:
    report = {"purpose": "data_feasibility_only_not_a_stock_screen", "probes": []}
    history_start = (now.astimezone(NY).date() - timedelta(days=1096)).isoformat()
    calendar = client.json("https://paper-api.alpaca.markets/v2/calendar",
                           {"start": history_start, "end": now.astimezone(NY).date().isoformat()})
    if not isinstance(calendar, list):
        raise ProbeError("unexpected_calendar_schema")
    sessions = completed_sessions(calendar, now)
    if not sessions:
        raise ProbeError("no_completed_market_session")
    latest = sessions[-1]
    opened, closed = session_times(latest)
    report.update(session=latest, calendar_sessions=len(sessions),
                  cutoff_policy="only_sessions_closed_at_least_30_minutes_ago",
                  asset_status={})
    for symbol in SYMBOLS:
        try:
            asset = client.json("https://paper-api.alpaca.markets/v2/assets/" + symbol)
            report["asset_status"][symbol] = {k: asset.get(k) for k in ("status", "exchange", "tradable")}
        except ProbeError as exc:
            report["asset_status"][symbol] = {"error": exc.summary()}

    def probe(name, kind, symbols, start, end, selected_sessions, feed="sip", timeframe=None,
              limit=1000, max_pages=30, adjustment="raw"):
        rows, meta = fetch_pages(client, kind, symbols, start, end, feed=feed,
                                 timeframe=timeframe, limit=limit, max_pages=max_pages,
                                 adjustment=adjustment)
        meta["name"] = name
        meta["quality"] = quality(rows, kind, selected_sessions, timeframe=timeframe,
                                   complete=meta["complete"], start=start, end=end)
        report["probes"].append(meta)
        # 生データはこの関数の中で扱い、戻り値やログへ出さない。
        return rows, meta

    first_open = session_times(sessions[0])[0].replace(hour=0, minute=0)
    raw_daily, daily = probe("sip_daily_3year", "bars", SYMBOLS, first_open, closed,
                     sessions, timeframe="1Day", limit=500)
    split_daily, split_meta = probe("sip_daily_3year_split_adjusted", "bars", SYMBOLS,
                                    first_open, closed, sessions, timeframe="1Day",
                                    limit=500, adjustment="split")
    report["split_adjustment_audit"] = split_adjustment_audit(
        raw_daily, split_daily, complete=daily["complete"] and split_meta["complete"])
    del raw_daily, split_daily
    minute_sessions = sessions[-20:]
    minute_start = session_times(minute_sessions[0])[0]
    minutes, minute_meta = probe("sip_minutes_20sessions", "bars", SYMBOLS,
                                minute_start, closed-timedelta(microseconds=1), minute_sessions,
                                timeframe="1Min", limit=10000, max_pages=20)
    report["missing_minute_audits"] = []
    if minute_meta["complete"]:
        for symbol in SYMBOLS:
            for gap in missing_minute_samples(minutes[symbol], minute_sessions):
                trades, meta = fetch_pages(client, "trades", (symbol,), gap,
                                           gap+timedelta(minutes=1)-timedelta(microseconds=1),
                                           feed="sip", limit=1000, max_pages=5)
                data = trades[symbol]
                only_odd_lot = bool(data) and all("I" in (t.get("c") or []) for t in data)
                reason = "inconclusive"
                if meta["complete"]:
                    if not data:
                        reason = "no_trades_returned_for_sample"
                    elif only_odd_lot:
                        reason = "trades_exist_all_have_odd_lot_condition"
                    else:
                        reason = "trades_exist_bar_absent_requires_condition_review"
                report["missing_minute_audits"].append({
                    "symbol": symbol, "minute": iso(gap), "trade_records": len(data),
                    "complete": meta["complete"], "status": meta["status"],
                    "interpretation": reason, "only_odd_lot_condition": only_odd_lot,
                    "http_requests": meta["http_requests"], "received_bytes": meta["received_bytes"],
                })
    del minutes
    # 単一取引所は比較試験として明示。全米市場の代替として自動採用しない。
    probe("iex_minutes_comparison", "bars", SYMBOLS, opened, closed-timedelta(microseconds=1),
          [latest], feed="iex", timeframe="1Min", limit=500, max_pages=10)
    tick_start = opened + timedelta(minutes=30)
    tick_end = tick_start + timedelta(minutes=1) - timedelta(microseconds=1)
    for kind in ("trades", "quotes"):
        probe("sip_" + kind + "_one_minute", kind, SYMBOLS, tick_start, tick_end, [],
              limit=1000, max_pages=20)
    # 現在の試験銘柄だけで古い履歴を確認。旧社名・旧コードは自動接続しない。
    old_start = datetime(2016, 1, 5, tzinfo=NY)
    probe("sip_daily_2016_sample", "bars", ("MU",), old_start,
          old_start+timedelta(days=3), [], timeframe="1Day", limit=100, max_pages=2)
    bytes_per_session_symbol = None
    if minute_meta["complete"] and minute_meta["records"]:
        bytes_per_session_symbol = minute_meta["received_bytes"] / (len(SYMBOLS)*len(minute_sessions))
    report["load_estimate"] = {
        "sample_based_uncompressed_response_bytes_per_symbol_session": round(bytes_per_session_symbol) if bytes_per_session_symbol else None,
        "approx_300_symbols_252_sessions_bytes": round(bytes_per_session_symbol*300*252) if bytes_per_session_symbol else None,
        "limitations": "five_current_symbols_only; includes_between_session_records; excludes_filings_storage_and_index_overhead; not_a_quote_or_invoice",
    }
    report["required_market_probe_ok"] = all(
        p["status"] == "ok" and p["records"] > 0
        and all(q["records"] > 0 and q["invalid_records"] == 0
                and q["identical_rows_or_duplicate_bar_times"] == 0
                and q["out_of_order"] == 0 and q["outside_requested_interval"] == 0
                for q in p["quality"].values())
        for p in (daily, minute_meta)
    )
    return report
