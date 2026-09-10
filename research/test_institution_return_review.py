"""Checks for timing leakage, pseudo-replication, and missing-label handling."""
import unittest

import institution_return_review as review


def trade(symbol, value, day="2026-06-02", status="priced"):
    return {"symbol": symbol, "net_return": value, "signal_date": day,
            "net_excess_over_benchmark": value, "status": status}


class InstitutionReviewChecks(unittest.TestCase):
    def test_public_snapshot_date_itself_cannot_be_used(self):
        rows = [trade("A", .1, d) for d in
                ("2026-05-29", "2026-06-01", "2026-06-02", "2026-06-30", "2026-07-01")]
        chosen = review.select_trades(rows, "2026-05-01", "2026-06-30", "2026-06-01")
        self.assertEqual([r["signal_date"] for r in chosen], ["2026-06-02", "2026-06-30"])

    def test_repeated_stock_is_not_extra_independent_evidence(self):
        rows = [trade("A", .1)] * 9 + [trade("B", -.1)]
        result = review.summarize(rows)
        self.assertAlmostEqual(result["symbol_equal_mean_net_return"], 0)
        self.assertAlmostEqual(result["trade_equal_mean_net_return"], .08)
        self.assertEqual(result["priced_symbols"], 2)
        self.assertEqual(result["symbol_equal_mean_win_fraction"], .5)

    def test_unknown_labels_and_unpriced_trades_are_not_losses(self):
        holdings = {"A": {"observed_reported_increase": True},
                    "B": {"observed_reported_increase": False}}
        rows = [trade("A", .1), trade("B", -.1), trade("C", .2), trade("C", None, status="unresolved")]
        result = review.analyze(rows, holdings, {"A", "B", "C"}, 1)
        groups = result["observed_reported_increase"]["groups"]
        self.assertEqual(groups["unknown"]["selected_trades"], 2)
        self.assertEqual(groups["unknown"]["priced_trades"], 1)
        self.assertEqual(groups["unknown"]["trade_equal_win_fraction"], 1)
        self.assertEqual(groups["nonincrease"]["selected_trades"], 1)
        self.assertEqual(result["matched_manager_increase"]["groups"]["unknown"]["selected_trades"], 4)

    def test_empty_and_invalid_labels(self):
        self.assertIsNone(review.summarize([])["symbol_equal_mean_net_return"])
        self.assertEqual(review.classify(None, "label"), "unknown")
        with self.assertRaises(ValueError):
            review.classify({"label": 0}, "label")
        with self.assertRaises(ValueError):
            review.summarize([trade("A", float("nan"))])

    def test_correlation_sign_and_undefined_cases(self):
        self.assertAlmostEqual(review.correlation([.1, .1], [-.1, -.1]), 1)
        self.assertAlmostEqual(review.correlation([-.1, -.1], [.1, .1]), -1)
        self.assertIsNone(review.correlation([.1], []))
        self.assertIsNone(review.correlation([.1], [.1]))

    def test_bootstrap_known_difference_and_small_sample_guard(self):
        a = review.summarize([trade(f"A{i}", .1) for i in range(5)])
        b = review.summarize([trade(f"B{i}", -.1) for i in range(5)])
        first = review.contrast(a, b, 7, repetitions=100)
        self.assertEqual(first, review.contrast(a, b, 7, repetitions=100))
        self.assertAlmostEqual(first["mean_net_return"]["difference"], .2)
        for boundary in first["mean_net_return"]["ci95"]:
            self.assertAlmostEqual(boundary, .2)
        small = review.summarize([trade("B", -.1)])
        self.assertIsNone(review.contrast(a, small, 7)["mean_net_return"]["ci95"])

    def test_frozen_evidence_is_available_without_network_or_credentials(self):
        returns, holdings, universe = review.load_frozen()
        self.assertEqual(holdings["outcome_as_of"], "2026-06-01")
        self.assertEqual(returns["primary_holding_sessions"], 10)
        self.assertEqual(len(universe["symbols"]), 54)


if __name__ == "__main__":
    unittest.main()
