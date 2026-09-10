"""Describe holdings-proxy/alert-return association; never infer buyer identity.

Runs offline using frozen, already-public derived evidence. See the protocol for
timing, weighting, and inference limits. Does not change any trading condition.
"""
import hashlib
import json
import math
import random
from collections import defaultdict
from pathlib import Path
from statistics import fmean, median


ROOT = Path(__file__).resolve().parents[1]
INPUTS = {
    "docs/evidence/next-returns-2026-09-10.json":
        "74f16e044fbea287212ca0ad177875ddbf0f40a67ddce6fbe2fdb621875765c1",
    "docs/evidence/batch-holdings-2026-09-10.json":
        "97d19c20e668ae1bdc676bd69f2a9662fb8fe0858a1fee135e8a0ab269e9a9f9",
    "docs/evidence/batch-comparison-protocol-2026-09-10.json":
        "ebb17d254790110735dd1de7952b88773f3139b462c3742dbaa2f853b66c3a92",
    "docs/INSTITUTION_RETURN_PROTOCOL.md":
        "8b26842c694c3e7d5e42c2bd7d02e679af55638a4a165a71f9ed48a0d0da98fc",
}
PROXIES = ("observed_reported_increase", "matched_manager_increase")
MODELS = ("repeated_up", "repeated", "abnormal")


def load_frozen(root=ROOT):
    result = []
    for path, expected in INPUTS.items():
        raw = (root / path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError(f"input_changed:{path}")
        if path.endswith(".json"):
            result.append(json.loads(raw))
    return result


def select_trades(rows, start, end, public_as_of=None):
    """Require a full calendar day after the snapshot cutoff for public use."""
    return [row for row in rows
            if start <= row["signal_date"] <= end
            and (public_as_of is None or row["signal_date"] > public_as_of)]


def classify(row, proxy):
    value = row.get(proxy) if row is not None else None
    if value is None:
        return "unknown"
    if type(value) is not bool:
        raise ValueError("proxy_must_be_boolean_or_null")
    return "increase" if value else "nonincrease"


def summarize(rows):
    priced = [r for r in rows if r["status"] == "priced"]
    by_symbol = defaultdict(list)
    for r in priced:
        if not isinstance(r.get("net_return"), (int, float)) or not math.isfinite(r["net_return"]):
            raise ValueError("invalid_priced_return")
        by_symbol[r["symbol"]].append(r)
    stocks = []
    for symbol, trades in sorted(by_symbol.items()):
        values = [r["net_return"] for r in trades]
        excess = [r["net_excess_over_benchmark"] for r in trades
                  if r.get("net_excess_over_benchmark") is not None]
        stocks.append({"symbol": symbol, "trades": len(trades),
                       "mean_net_return": fmean(values),
                       "win_fraction": fmean(v > 0 for v in values),
                       "mean_net_excess": fmean(excess) if excess else None})
    values = [r["net_return"] for r in priced]
    excess_stocks = [s["mean_net_excess"] for s in stocks if s["mean_net_excess"] is not None]
    return {
        "selected_trades": len(rows), "priced_trades": len(priced),
        "unresolved_trades": len(rows) - len(priced), "priced_symbols": len(stocks),
        "symbol_equal_mean_net_return": fmean(s["mean_net_return"] for s in stocks) if stocks else None,
        "symbol_equal_mean_win_fraction": fmean(s["win_fraction"] for s in stocks) if stocks else None,
        "symbol_equal_mean_net_excess": fmean(excess_stocks) if excess_stocks else None,
        "symbols_with_benchmark": len(excess_stocks),
        "trade_equal_mean_net_return": fmean(values) if values else None,
        "trade_equal_median_net_return": median(values) if values else None,
        "trade_equal_win_fraction": fmean(v > 0 for v in values) if values else None,
        "trade_wins": sum(v > 0 for v in values),
        "by_symbol": stocks,
    }


def quantile(values, p):
    ordered = sorted(values)
    pos = p * (len(ordered) - 1)
    lower = math.floor(pos)
    upper = math.ceil(pos)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (pos - lower)


def correlation(increased, nonincreased):
    if not increased or not nonincreased:
        return None
    x = [1] * len(increased) + [0] * len(nonincreased)
    y = increased + nonincreased
    mx, my = fmean(x), fmean(y)
    denominator = math.sqrt(sum((v - mx)**2 for v in x) * sum((v - my)**2 for v in y))
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / denominator if denominator else None


def contrast(increase, nonincrease, seed, repetitions=10000):
    a, b = increase["by_symbol"], nonincrease["by_symbol"]
    metrics = ("mean_net_return", "win_fraction", "mean_net_excess")
    output = {}
    for metric in metrics:
        av = [s[metric] for s in a if s[metric] is not None]
        bv = [s[metric] for s in b if s[metric] is not None]
        if not av or not bv:
            output[metric] = {"difference": None, "ci95": None,
                              "ci_reason": "one_or_both_groups_empty", "leave_one_symbol_out_range": None}
            continue
        difference = fmean(av) - fmean(bv)
        leave_one = []
        if len(av) > 1 and len(bv) > 1:
            leave_one = [(sum(av) - v) / (len(av) - 1) - fmean(bv) for v in av]
            leave_one += [fmean(av) - (sum(bv) - v) / (len(bv) - 1) for v in bv]
        interval = None
        if min(len(av), len(bv)) >= 5:
            rng = random.Random(seed)
            samples = [fmean(rng.choices(av, k=len(av))) - fmean(rng.choices(bv, k=len(bv)))
                       for _ in range(repetitions)]
            interval = [quantile(samples, .025), quantile(samples, .975)]
        output[metric] = {"difference": difference, "ci95": interval,
                          "ci_reason": "conditional_symbol_bootstrap" if interval else "fewer_than_5_symbols_in_a_group",
                          "leave_one_symbol_out_range": [min(leave_one), max(leave_one)] if leave_one else None}
    output["point_biserial_return_correlation"] = correlation(
        [s["mean_net_return"] for s in a], [s["mean_net_return"] for s in b])
    return output


def analyze(rows, holdings, scope, seed):
    chosen = [r for r in rows if r["symbol"] in scope]
    output = {"total": summarize(chosen), "strict_quality_clear_symbols": sorted(
        {r["symbol"] for r in chosen if holdings.get(r["symbol"], {}).get("strict_quality_clear")})}
    for proxy in PROXIES:
        groups = {name: [] for name in ("increase", "nonincrease", "unknown")}
        for row in chosen:
            groups[classify(holdings.get(row["symbol"]), proxy)].append(row)
        summaries = {name: summarize(group) for name, group in groups.items()}
        if sum(s["selected_trades"] for s in summaries.values()) != len(chosen):
            raise ValueError("group_accounting_error")
        output[proxy] = {"groups": summaries,
                         "increase_minus_nonincrease": contrast(summaries["increase"], summaries["nonincrease"], seed)}
    return output


def main():
    returns, holdings_data, universe = load_frozen()
    if holdings_data["outcome_as_of"] != "2026-06-01":
        raise ValueError("public_snapshot_cutoff_changed")
    if returns["primary_holding_sessions"] != 10 or returns["primary_cost_each_side"] != .0025:
        raise ValueError("return_convention_changed")
    holdings = {r["symbol"]: r for r in holdings_data["rows"]}
    symbols = {r["symbol"] for r in universe["symbols"]}
    low = {r["symbol"] for r in universe["symbols"] if r["primary_low_reported_ratio"]}
    if len(holdings) != len(holdings_data["rows"]) or set(holdings) != symbols:
        raise ValueError("holdings_universe_mismatch")
    disagreements = [s for s in sorted(symbols) if all(type(holdings[s].get(p)) is bool for p in PROXIES)
                     and holdings[s][PROXIES[0]] != holdings[s][PROXIES[1]]]
    result = {
        "version": "institution-return-review-0.1", "input_sha256": INPUTS,
        "analysis_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "holdings_periods": holdings_data["periods"], "holdings_public_as_of": holdings_data["outcome_as_of"],
        "primary_model": "repeated_up", "bootstrap_repetitions": 10000,
        "bootstrap_base_seed": 20260910, "proxy_disagreement_symbols": disagreements,
        "holdings_quality": {"universe_symbols": len(symbols),
                             "strict_quality_clear": sum(r["strict_quality_clear"] for r in holdings.values()),
                             "actual_buyer_identity_observed": False},
        "proxy_coverage": {p: {name: sum(classify(r, p) == name for r in holdings.values())
                               for name in ("increase", "nonincrease", "unknown")} for p in PROXIES},
        "scopes": {"all": sorted(symbols), "primary_low_reported_ratio": sorted(low)},
        "periods": {},
        "limitations": ["quarter_end_holdings_are_not_daily_buyer_identity",
                        "no_strictly_quality_cleared_holdings_labels",
                        "retrospective_association_is_not_an_available_signal",
                        "post_publication_is_an_existing_ledger_slice_not_a_fresh_strategy_backtest",
                        "previously_examined_returns_not_independent_validation",
                        "symbol_bootstrap_does_not_resolve_calendar_or_sector_dependence",
                        "neither_causal_effect_nor_market_representative_sample",
                        "price_only_returns_assumed_costs_no_verified_execution_or_portfolio_constraints"],
    }
    phases = (("retrospective_q1", "exploration", "2026-01-01", "2026-03-31", None),
              ("after_publication_june", "validation", "2026-06-02", "2026-06-30", "2026-06-01"))
    for phase, source, start, end, cutoff in phases:
        result["periods"][phase] = {"start": start, "end": end, "models": {}}
        for model in MODELS:
            source_rows = returns[source]["trade_ledger_10"][model]
            keys = [(r["symbol"], r["signal_date"]) for r in source_rows]
            if len(keys) != len(set(keys)):
                raise ValueError("duplicate_trade")
            rows = select_trades(source_rows, start, end, cutoff)
            if any(r["symbol"] not in symbols or r["holding_sessions"] != 10 for r in rows):
                raise ValueError("unexpected_trade_scope")
            seed = 20260910 + int.from_bytes(hashlib.sha256((phase + model).encode()).digest()[:4], "big")
            model_result = {name: analyze(rows, holdings, set(scope), seed)
                            for name, scope in result["scopes"].items()}
            result["periods"][phase]["models"][model] = model_result
    destination = ROOT / "diagnostics/institution-return-review.json"
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    for phase, phase_data in result["periods"].items():
        for model, model_data in phase_data["models"].items():
            for proxy in PROXIES:
                review = model_data["all"][proxy]
                print(json.dumps({"phase": phase, "model": model, "proxy": proxy,
                    "groups": {g: {k: v for k, v in s.items() if k != "by_symbol"}
                               for g, s in review["groups"].items()},
                    "contrast": review["increase_minus_nonincrease"]}))


if __name__ == "__main__":
    main()
