#!/usr/bin/env python3
"""Offline checks for the unapproved, telemetry-only M6 draft. Not a CI gate."""
import copy
import csv
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import score
import run

ROWS = {'telemetry_adapter', 'telemetry_context', 'telemetry_metrics', 'telemetry_instrumentation_lifecycle'}
PROBES = {'telemetry_' + s for s in ('plain', 'print', 'external', 'manual', 'error', 'dump_load')}
BACKENDS = ['vm', 'dart2js', 'dart2wasm']


class DraftTargetTest(unittest.TestCase):
    def setUp(self):
        self.target = json.loads((Path(__file__).parent / 'm6-target.json').read_text())
        self.rules = json.loads(score.RULES.read_text())['rows']

    def test_only_four_rows_with_all_backends_and_no_allowances(self):
        self.assertIn('draft', self.target['about'].lower())
        self.assertEqual(set(self.target['rows']), ROWS)
        scope = {r['cap_id']: r for r in csv.DictReader(score.MANIFEST.read_text().splitlines(), delimiter='\t')
                 if r['cap_id'] in ROWS | {'telemetry_flush'}}
        self.assertEqual(score.target_problems(self.target, scope), [])
        for row in self.target['rows'].values():
            self.assertEqual(row['backends'], BACKENDS)
            self.assertNotIn('allowed', row)
        self.assertEqual(self.target['excluded']['telemetry_flush']['clause'], 'U1')
        self.assertEqual(self.target['clauses']['U1'], {
            'text': 'the Python binding has no flush; core delivers synchronously', 'owner': 'upstream'})
        self.assertNotIn('cli_telemetry', self.target['rows'])

    def test_every_missing_backend_or_non_present_row_fails(self):
        statuses = {r: {b: ('PRESENT', 'measured') for b in BACKENDS} for r in ROWS}
        self.assertEqual(score.score_target(self.target, statuses, BACKENDS)[1], 0)
        for row in ROWS:
            for backend in BACKENDS:
                for status in ('ABSENT', 'PARTIAL', 'N/A', 'NO-OUTCOME'):
                    bad = copy.deepcopy(statuses)
                    bad[row][backend] = (status, 'measured')
                    self.assertEqual(score.score_target(self.target, bad, BACKENDS)[1], 1)

    def test_probes_are_owned_run_and_bound(self):
        self.assertTrue(PROBES <= set(run.core_probes()))
        manifest = json.loads((Path(__file__).parent / 'manifest.json').read_text())
        for row in ROWS:
            self.assertTrue(self.rules[row]['probes'])
            for pid in self.rules[row]['probes']:
                self.assertIn(pid, PROBES)
                self.assertIn('ref-py', manifest['probes'][pid]['outcomes'])


if __name__ == '__main__':
    unittest.main()
