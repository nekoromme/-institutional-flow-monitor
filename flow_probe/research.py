"""第2段階の検証用データを準備する。シグナルの採点や成績評価は行わない。"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .reference import documented_price_factor

NY = ZoneInfo("America/New_York")

PROTOCOL = {
    "version": "stage2-pilot-0.1",
    "purpose": "engineering_pilot_not_evidence_of_institutional_detection",
    "volume_scope": "alpaca_sip_provider_daily_bar_not_regular_session_only",
    "baseline_sessions": 60,
    "baseline_rule": "previous_60_full_length_market_sessions_excluding_current",
    "shortened_sessions": "exclude_from_signal_and_baseline_no_linear_scaling",
    "missing_or_zero_baseline": "unknown_do_not_zero_fill",
    "split_rule": "convert_prior_share_volume_to_decision_day_units_only",
    "candidate_rule_for_next_stage": "volume_strictly_above_nearest_rank_95th_percentile",
    "repeat_rule_for_next_stage": "current_day_abnormal_and_at_least_2_in_last_5_market_sessions",
    "repeat_missing_rule": "unknown_if_any_of_5_daily_judgments_unknown",
    "aggregate_score": None,
    "main_label": "split_adjusted_reported_holdings_change_in_containing_calendar_quarter",
    "label_unit": "symbol_calendar_quarter_not_individual_daily_alert",
    "return_horizon_sessions": 10,
    "entry_rule": "next_regular_open_after_actual_or_conservative_signal_availability",
    "pilot_symbols_are_not_low_ownership_certified": True,
    "model_selection_or_backtest_performed": False,
}


def prepare_daily(rows: dict, sessions: list, split_audit: dict) -> tuple[list, dict]:
    """公表前の保有情報を一切入れず、出来高の単位と過去窓だけを整える。"""
    days = [r["date"] for r in sessions]
    full_days = [r["date"] for r in sessions if r["open"] == "09:30" and r["close"] == "16:00"]
    full_set = set(full_days)
    output, diagnostics = [], {}
    window_length = PROTOCOL["baseline_sessions"]
    for symbol, bars in rows.items():
        by_day, duplicates = {}, set()
        for bar in bars:
            day = datetime.fromisoformat(bar["t"].replace("Z", "+00:00")).astimezone(NY).date().isoformat()
            if day in by_day:
                duplicates.add(day)
            by_day[day] = bar
        counts = {}
        prior = []
        for day in days:
            bar = by_day.get(day)
            history = prior[-window_length:]
            reason = None
            if split_audit.get(symbol, {}).get("status") != "comparable_sample":
                reason = "unresolved_adjustment"
            elif day not in full_set:
                reason = "shortened_session"
            elif len(history) < window_length:
                reason = "warmup"
            elif any(d in duplicates or d not in by_day for d in history+[day]):
                reason = "missing_or_duplicate_day"
            else:
                values = [by_day[d].get("v") for d in history+[day]]
                if not all(isinstance(v, (float, int)) and not isinstance(v, bool)
                           and math.isfinite(v) and v > 0 for v in values):
                    reason = "nonpositive_or_invalid_volume"
            item = {"symbol": symbol, "date": day, "status": reason or "prepared",
                    "baseline_first_day": history[0] if len(history) == window_length else None,
                    "raw_volume": bar.get("v") if bar else None}
            if not reason:
                # その判定日までの併合だけを反映。未来の併合を判定の入力にしない。
                item["baseline_volumes_in_decision_day_shares"] = [
                    by_day[d]["v"] / documented_price_factor(symbol, d, day) for d in history]
            output.append(item)
            counts[item["status"]] = counts.get(item["status"], 0)+1
            if day in full_set:
                prior.append(day)
        diagnostics[symbol] = counts
    return output, {"protocol": PROTOCOL, "rows": len(output), "by_symbol": diagnostics,
                    "all_institution_holdings_labels_ready": False,
                    "backtest_ready": False, "performance_results": None}


def save_prepared(rows: dict, sessions: list, split_audit: dict, destination: str) -> dict:
    prepared, report = prepare_daily(rows, sessions, split_audit)
    # 原数値を含むファイルは無視対象のdata/へ。公開用レポートには件数とハッシュだけ。
    body = json.dumps({"protocol": PROTOCOL, "records": prepared}, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode()
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    report.update(dataset_sha256=hashlib.sha256(body).hexdigest(), dataset_bytes=len(body),
                  persistence="local_file_only; GitHub_runner_copy_expires_after_job; not_uploaded")
    return report
