"""未来の資料を対象選びへ混ぜないことと、候補抽出の再現性を検査する。"""
import csv
import io
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from flow_probe.cohort import AS_OF, PERIOD, build_frame, frozen_states, observed_row, read_security_list, stable_batch
from flow_probe.holdings import select_denominator
from flow_probe.holdings_reference import reviewed_denominator
from test_holdings import filing, row


class CohortTests(unittest.TestCase):
    def original(self):
        return filing('before', [row(100)], filed='2025-11-14', period=PERIOD)

    def test_cutoff_drops_same_day_future_and_later_period(self):
        before = self.original()
        late = filing('after', [row(900)], filed=AS_OF, period=PERIOD, form='13F-HR/A',
                      amendment_type='RESTATEMENT', amendment_number=1)
        later_period = filing('later_period', [row(999)], filed='2026-02-15', period='2025-12-31')
        original, _ = frozen_states({'before': before})
        result, audit = frozen_states({f['accession']: f for f in [before, late, later_period]})
        self.assertEqual(result, original)
        self.assertEqual(audit['same_period_filings_excluded_at_or_after_cutoff'], 1)
        self.assertEqual(audit['later_period_filings_excluded'], 1)

    def test_december_correction_is_available_at_january_selection(self):
        before = self.original()
        correction = filing('correction', [row(80)], filed='2025-12-15', period=PERIOD,
                            form='13F-HR/A', amendment_type='RESTATEMENT', amendment_number=1)
        states, _ = frozen_states({f['accession']: f for f in [before, correction]})
        self.assertEqual(next(iter(states.values()))['rows'][0]['shares'], 80)

    def test_future_period_cannot_be_chosen(self):
        with self.assertRaises(ValueError):
            frozen_states({}, period='2026-03-31')

    def test_future_denominator_does_not_replace_known_shares(self):
        old = {'val': 36734690, 'end': PERIOD, 'filed': '2025-11-14', 'form': '10-Q',
               'accn': '0001437749-25-035221'}
        later = {**old, 'val': 57346783, 'filed': AS_OF, 'accn': 'future'}
        concepts = {'us-gaap': {'source_url': 'https://data.sec.gov/example', 'units': {'shares': [old, later]}}}
        chosen = select_denominator(concepts, PERIOD, AS_OF)
        self.assertEqual(chosen['shares'], 36734690)
        self.assertTrue(reviewed_denominator('UAVS', chosen)['security_class_verified'])
        changed = {**chosen, 'shares': 36734691}
        self.assertFalse(reviewed_denominator('UAVS', changed)['security_class_verified'])

    def test_hash_order_never_depends_on_input_order_or_outcomes(self):
        frame = [{'cusip': str(i).zfill(9)} for i in range(300)]
        selected = [r['cusip'] for r in stable_batch(frame)]
        altered = [{**r, 'future_return': i*1000, 'future_holdings': 999} for i, r in enumerate(reversed(frame))]
        self.assertEqual([r['cusip'] for r in stable_batch(altered)], selected)
        self.assertEqual(len(selected), 200)
        with self.assertRaises(ValueError):
            stable_batch(frame + frame[:1])

    def test_option_indicator_is_not_option_position_or_deleted_security(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'list.txt'
            p.write_text('00848K309*' + 'EXAMPLE'.ljust(30) + 'COM SHS'.ljust(27) + '   ' + '\n')
            official = read_security_list(p)
            data = {'CUSIP': '00848K309', 'SSHPRNAMTTYPE': 'SH', 'SSHPRNAMT': '10',
                    'PUTCALL': '', 'TITLEOFCLASS': 'COM SHS'}
            self.assertTrue(observed_row(data, official))
            for changes in [{'PUTCALL': 'CALL'}, {'SSHPRNAMTTYPE': 'PRN'}, {'SSHPRNAMT': '0'},
                            {'TITLEOFCLASS': 'PREFERRED'}, {'CUSIP': 'unknown'}]:
                self.assertFalse(observed_row({**data, **changes}, official))
            official['00848K309']['status'] = '*D*'
            self.assertFalse(observed_row(data, official))

    def test_frame_stream_excludes_future_rows_preserves_unobserved_as_unknown(self):
        before = self.original()
        after = filing('future', [row(900)], filed='2026-02-15', period='2025-12-31')
        states, _ = frozen_states({'before': before, 'future': after})
        official = {c: {'cusip': c, 'issuer': 'Example', 'class': 'COM', 'status': ''}
                    for c in ['00848K309', '747906600']}
        def tsv(headers, rows):
            out = io.StringIO(); writer = csv.writer(out, delimiter='\t');writer.writerow(headers);writer.writerows(rows)
            return out.getvalue()
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'fixture.zip'
            with ZipFile(p, 'w') as z:
                z.writestr('SUBMISSION.tsv', tsv(['ACCESSION_NUMBER'], [['before'], ['future']]))
                z.writestr('INFOTABLE.tsv', tsv(['ACCESSION_NUMBER','CUSIP','SSHPRNAMTTYPE',
                                              'SSHPRNAMT','PUTCALL','TITLEOFCLASS'],
                                             [['before','00848K309','SH','100','','COM'],
                                              ['future','747906600','SH','999','','COM']]))
            frame, stats = build_frame([p], states, official)
        self.assertEqual([r['cusip'] for r in frame], ['00848K309'])
        self.assertIsNone(frame[0]['low_ownership_eligible'])
        self.assertEqual(stats['official_common_without_accepted_observation'], 1)
        self.assertTrue(stats['unobserved_is_not_zero_ownership'])


if __name__ == '__main__':
    unittest.main()
