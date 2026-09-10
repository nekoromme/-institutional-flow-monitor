import gzip
import unittest

from research.history.indexes import index_records
from research.history.security_lists import parse_list


class HistoricalSourceChecks(unittest.TestCase):
    def fixture(self, rows, total=None, run_date='10/05/2022'):
        return '\n'.join([
            'Year: 2022 Qtr: 3', f'Run Date: {run_date}', *rows,
            f'Total Count: {len(rows) if total is None else total}',
        ])

    def test_security_class_and_repeated_option_eligibility_marker(self):
        rows, stats = parse_list(self.fixture([
            '123456 78 9 * EXAMPLE INC    COM',
            '123456 78 9   EXAMPLE INC    COM',
            '222222 22 2   OLD INC        COM SHS DELETED',
            '333333 33 3   OTHER INC      PFD',
        ]), 2022)
        self.assertEqual(rows, {'123456789': True, '222222222': False, '333333333': False})
        self.assertEqual(stats['repeated_security_numbers'], 1)

    def test_incomplete_conflicting_or_late_lists_are_rejected(self):
        row = '123456 78 9   EXAMPLE INC    COM'
        for fixture in [
            self.fixture([row], total=2),
            self.fixture([row, '123456 78 9   EXAMPLE INC    PFD']),
            self.fixture([row], run_date='01/05/2023'),
        ]:
            with self.subTest(fixture=fixture), self.assertRaises(ValueError):
                parse_list(fixture, 2022)

    def test_index_cutoff_and_duplicate_filings(self):
        records = [
            '123|Before|10-K|2022-12-31|edgar/data/123/before.txt',
            '123|Before|10-K|2022-12-31|edgar/data/123/before.txt',
            '456|Future|10-K|2023-01-01|edgar/data/456/future.txt',
            '789|Too old|10-K|2021-12-31|edgar/data/789/old.txt',
        ]
        parsed = index_records(gzip.compress('\n'.join(records).encode()), 2023)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]['cik'], '0000000123')
        self.assertEqual(parsed[0]['filed'], '2022-12-31')


if __name__ == '__main__':
    unittest.main()
