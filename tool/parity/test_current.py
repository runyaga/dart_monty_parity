"""Offline current-baseline round trips, using real outcome files and CLI scoring."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import score

BACKENDS = ('vm', 'dart2js', 'dart2wasm')


class CurrentBaselineTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.out = self.root / 'out'
        self.expected = self.root / 'expected'
        self.baseline = self.root / 'current.json'
        self.rules = self.root / 'rows.json'
        self.manifest = self.root / 'manifest.tsv'
        self.out.mkdir()
        (self.out / 'platform.json').write_text('{"arch": "arm64"}')
        self.rules.write_text(json.dumps({
            'rows': {'measured': {'cases': ['a', 'b', 'c'], 'compare': ['V'],
                                  'expect': {'ffi': 'ABSENT', 'web': 'ABSENT'}},
                     'worker_only': {'worker': {}}},
            'platforms': {'arm64': {}, 'x86_64': {}},
        }))
        self.manifest.write_text(
            'cap_id\tin_scope\tbaseline_ffi\tbaseline_web\n'
            'measured\tyes\tABSENT\tABSENT\n'
            'unmeasured\tyes\tPRESENT\tPRESENT\n'
            'worker_only\tyes\tABSENT\tABSENT\n')
        for cid in ('a', 'b', 'c'):
            (self.expected / cid).mkdir(parents=True)
            (self.expected / cid / 'ref-py.json').write_text('{"steps": [{"value": 1}]}')
            (self.out / cid).mkdir()
            for b in BACKENDS:
                (self.out / cid / f'core-{b}.json').write_text(
                    json.dumps({'steps': [{'value': 2 if cid == 'c' else 1}]}))

    def cli(self, *flags):
        output = io.StringIO()
        with mock.patch.object(score, 'RULES', self.rules), \
             mock.patch.object(score, 'MANIFEST', self.manifest), \
             mock.patch.object(sys, 'argv', ['score.py', str(self.out), '--expected', str(self.expected), *flags]), \
             contextlib.redirect_stdout(output):
            rc = score.main()
        return rc, output.getvalue()

    def write(self, *flags):
        rc, output = self.cli('--write-current', str(self.baseline), *flags)
        self.assertEqual(rc, 0, output)
        return json.loads(self.baseline.read_text())

    def assert_current(self, *flags):
        rc, output = self.cli('--assert-current', str(self.baseline), *flags)
        self.assertEqual(rc, 0, output)

    def test_new_file_round_trip_all_backends_and_manifest_totals(self):
        doc = self.write()
        self.assert_current()
        self.assertEqual(doc['present'], dict.fromkeys(BACKENDS, 1))
        self.assertEqual(set(doc['rows']), {'measured'})
        for b in BACKENDS:
            self.assertEqual(doc['rows']['measured'][b], 'PARTIAL')
            self.assertEqual(doc['totals'][b], {'PARTIAL': 1, 'PRESENT': 1, 'ABSENT': 1})
        self.assertIn('historical ABSENT', self.cli()[1])

    def test_refreshes_stale_platform_rows_cells_and_totals_keeps_metadata(self):
        doc = self.write()
        doc['subject'] = 'test recording'
        doc['deltas'] = [{'row': 'measured', 'why': 'intentional test change'}]
        doc['platforms']['arm64'] = {
            'measured_on': 'host', 'present': dict.fromkeys(BACKENDS, 99),
            'rows': {'measured': dict.fromkeys(BACKENDS, 'ABSENT')},
            'cells': {'measured': {'a': dict.fromkeys(BACKENDS, 'DIVERGE')}},
            'totals': {b: {'PRESENT': 99} for b in BACKENDS},
        }
        self.baseline.write_text(json.dumps(doc))
        updated = self.write()
        self.assert_current()
        for field in ('subject', 'deltas'):
            self.assertEqual(updated[field], doc[field])
        self.assertEqual(updated['platforms']['arm64']['measured_on'], 'host')
        self.assertEqual(updated['platforms']['arm64']['present'], updated['present'])
        self.assertEqual(updated['platforms']['arm64']['totals'], updated['totals'])

    def test_partial_backend_recording_preserves_other_backends(self):
        before = self.write()
        (self.out / 'c/core-vm.json').write_text('{"steps": [{"value": 1}]}')
        after = self.write('--backends', 'vm')
        self.assert_current()
        self.assertEqual(after['present']['vm'], 2)
        for b in ('dart2js', 'dart2wasm'):
            for field in ('present', 'totals'):
                self.assertEqual(after[field][b], before[field][b])
            self.assertEqual(after['rows']['measured'][b], before['rows']['measured'][b])
            for cid in ('a', 'b', 'c'):
                self.assertEqual(after['cells']['measured'][cid][b], before['cells']['measured'][cid][b])

    def test_recording_one_architecture_preserves_other_architecture(self):
        self.write()
        (self.out / 'platform.json').write_text('{"arch": "x86_64"}')
        self.write()
        (self.out / 'platform.json').write_text('{"arch": "arm64"}')
        (self.out / 'c/core-vm.json').write_text('{"steps": [{"value": 1}]}')
        self.write()
        self.assert_current()
        (self.out / 'platform.json').write_text('{"arch": "x86_64"}')
        (self.out / 'c/core-vm.json').write_text('{"steps": [{"value": 2}]}')
        self.assert_current()
        self.write('--backends', 'vm')
        self.assert_current()
        (self.out / 'platform.json').write_text('{"arch": "arm64"}')
        (self.out / 'c/core-vm.json').write_text('{"steps": [{"value": 1}]}')
        self.assert_current()

    def test_missing_outcome_fails_even_if_row_and_total_stay_the_same(self):
        self.write()
        (self.out / 'b/core-dart2wasm.json').unlink()
        rc, output = self.cli('--assert-current', str(self.baseline))
        self.assertEqual(rc, 1)
        self.assertIn('FAIL current case b (row measured) on dart2wasm: measured NO-OUTCOME, recorded MATCH', output)
        self.assertNotIn('FAIL current row', output)
        self.assertNotIn('FAIL current PRESENT', output)


if __name__ == '__main__':
    unittest.main()
