"""現金・保有の時系列で年利を水増ししないことを確認する。"""
import unittest
from research.history.annual_portfolio import simulate


def books(prices):
    return {s: {d: {'valid': True, 'split': {'o': p, 'c': p}}
                for d, p in rows.items()} for s, rows in prices.items()}


def attempt(s, entry, exit):
    return {'symbol': s, 'entry_date': entry, 'exit_date': exit}


class PortfolioTests(unittest.TestCase):
    def test_idle_cash_not_treated_as_invested(self):
        summary, _ = simulate([attempt('A', '1', '2')],
            books({'A': {'1': 100, '2': 110}}), ['1', '2'], cost=0)
        self.assertAlmostEqual(summary['annual_return'], .01)

    def test_fees_in_cash_and_quantity(self):
        summary, _ = simulate([attempt('A', '1', '2')],
            books({'A': {'1': 100, '2': 100}}), ['1', '2'])
        self.assertAlmostEqual(summary['annual_return'], .1*((1-.0025)/(1+.0025)-1))

    def test_close_sale_cannot_fund_same_day_open(self):
        summary, _ = simulate([attempt('A', '1', '2'), attempt('B', '2', '3')],
            books({'A': {'1': 100, '2': 100}, 'B': {'2': 100, '3': 100}}),
            ['1', '2', '3'], ticket=1, cost=0, limit=10)
        self.assertEqual(summary['purchases'], 1)
        self.assertEqual(summary['skipped']['insufficient_cash'], 1)

    def test_position_cap_and_open_year_end_valuation(self):
        summary, _ = simulate([attempt('A', '1', '3'), attempt('B', '1', '3')],
            books({'A': {'1': 100, '2': 120}, 'B': {'1': 100, '2': 100}}),
            ['1', '2'], cost=0, limit=1)
        self.assertEqual(summary['year_end_open_positions'], 1)
        self.assertEqual(summary['skipped']['position_limit'], 1)
        self.assertAlmostEqual(summary['annual_return'], .02)

    def test_drawdown_uses_peak_of_whole_account(self):
        summary, _ = simulate([attempt('A', '1', '3')],
            books({'A': {'1': 100, '2': 150, '3': 100}}), ['1', '2', '3'], cost=0)
        self.assertAlmostEqual(summary['max_close_drawdown'], 1/1.05-1)

    def test_missing_mark_fails_instead_of_zero_fill(self):
        with self.assertRaisesRegex(ValueError, 'missing_portfolio'):
            simulate([attempt('A', '1', '3')], books({'A': {'1':100}}), ['1','2'])

    def test_multiple_index_lots_kept_separately(self):
        summary, _ = simulate([attempt('A', '1', '2'), attempt('B', '1', '2')],
            books({'SPY': {'1':100, '2':110}}), ['1','2'], substitute='SPY', cost=0)
        self.assertEqual(summary['purchases'], 2)
        self.assertAlmostEqual(summary['annual_return'], .02)
