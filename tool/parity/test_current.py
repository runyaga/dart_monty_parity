"""Offline current-baseline round trips, using real outcome files and CLI scoring."""
from __future__ import annotations

import contextlib
from collections import Counter
import csv
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


class RepositoryBaselineTest(unittest.TestCase):
    def setUp(self):
        self.doc = json.loads((score.ROOT / 'tool/parity/baseline-current.json').read_text())

    def rows(self, arch):
        overrides = self.doc['platforms'][arch].get('rows', {})
        return {row: {**values, **overrides.get(row, {})}
                for row, values in self.doc['rows'].items()}

    def cells(self, arch):
        overrides = self.doc['platforms'][arch].get('cells', {})
        return {row: {case: {**values, **overrides.get(row, {}).get(case, {})}
                      for case, values in cases.items()}
                for row, cases in self.doc['cells'].items()}

    def test_recorded_statuses_are_known(self):
        # A partial backend/architecture recording explicitly leaves new rows
        # unmeasured elsewhere, rather than copying the new VM measurement.
        for arch in self.doc['platforms']:
            for row, values in self.rows(arch).items():
                self.assertFalse(set(values.values()) - {None, 'PRESENT', 'PARTIAL', 'ABSENT', 'REFUSED', 'UNVERIFIED'}, row)

    def test_recorded_present_rows_have_only_matching_cells(self):
        for arch in self.doc['platforms']:
            cells = self.cells(arch)
            for row, values in self.rows(arch).items():
                for backend, status in values.items():
                    if status == 'PRESENT':
                        recorded = [c[backend] for c in cells.get(row, {}).values() if c.get(backend) is not None]
                        self.assertTrue(recorded, (arch, row, backend))
                        self.assertEqual(set(recorded), {'MATCH'}, (arch, row, backend))

    def test_platform_totals_include_unmeasured_manifest_rows(self):
        with score.MANIFEST.open() as source:
            manifest = [r for r in csv.DictReader(source, delimiter='\t')
                        if r['in_scope'] == 'yes']
        rules = json.loads(score.RULES.read_text())['rows']
        measured = {row for row, rule in rules.items() if 'cases' in rule or 'probes' in rule or 'allowed' in rule}
        for arch, platform in self.doc['platforms'].items():
            rows = self.rows(arch)
            self.assertFalse(set(rows) - measured)
            for backend in BACKENDS:
                # Match score.main: measured rows replace manifest statuses;
                # unmeasured (including worker-only) rows retain the manifest.
                column = 'baseline_ffi' if backend == 'vm' else 'baseline_web'
                counts = Counter(rows.get(r['cap_id'], {}).get(backend) or r[column] for r in manifest)
                with self.subTest(arch=arch, backend=backend):
                    self.assertEqual(platform['present'][backend], counts['PRESENT'])
                    self.assertEqual(platform['totals'][backend], dict(counts))


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
             mock.patch.object(score, 'case_index', return_value={**score.case_index(), 'a': {}, 'b': {}, 'c': {'fs': {'mounts': [{}]}}}), \
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

    def test_backend_specific_cases_round_trip_and_remove_stale_cells(self):
        before = self.write()
        rules = json.loads(self.rules.read_text())
        rules['rows']['measured']['cases_web'] = ['a', 'b']
        self.rules.write_text(json.dumps(rules))
        for backend in ('dart2js', 'dart2wasm'):
            (self.out / f'c/core-{backend}.json').write_text(json.dumps(
                {'refused': {'where': 'MountDir', 'type': 'UnsupportedError'}}))
        after = self.write()
        self.assert_current()
        self.assertEqual(after['rows']['measured'], {'vm': 'PARTIAL', 'dart2js': 'PRESENT', 'dart2wasm': 'PRESENT'})
        self.assertEqual(after['cells']['measured']['c'], {'vm': 'DIVERGE', 'dart2js': None, 'dart2wasm': None})
        self.assertEqual(before['cells']['measured']['c']['dart2js'], 'DIVERGE')

    def test_new_row_does_not_invent_other_architecture_measurements(self):
        self.write()
        (self.out / 'platform.json').write_text('{"arch": "x86_64"}')
        self.write()
        (self.out / 'platform.json').write_text('{"arch": "arm64"}')
        rules = json.loads(self.rules.read_text())
        rules['rows']['unmeasured'] = {'cases': ['a'], 'compare': ['V']}
        self.rules.write_text(json.dumps(rules))
        after = self.write('--backends', 'vm')
        self.assert_current('--backends', 'vm')
        self.assertIsNone(after['platforms']['x86_64']['rows']['unmeasured']['vm'])
        self.assertIsNone(after['platforms']['x86_64']['cells']['unmeasured']['a']['vm'])

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
