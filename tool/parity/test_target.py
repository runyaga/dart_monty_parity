#!/usr/bin/env python3
"""Self-test of the M3 parity target (tool/parity/m3-target.json; score.py --target), offline.

usage: python3 -I tool/parity/test_target.py

  - the committed file partitions the 185 in-scope manifest rows into target rows and excluded rows;
  - every exclusion names a clause, and the area clauses and the mount clause hold as the rule states them;
  - an `allowed` entry is one of the three kinds the design allows, with a reason and a cite;
  - the scorer passes a target row PRESENT on every backend it must be, shows (and counts) a row that is not,
    accepts a row an `allowed` entry covers, and fails an `allowed` entry whose row is now PRESENT;
  - a file that lists a row twice, or drops one, or carries an `allowed` entry of another kind, is reported.
"""

from __future__ import annotations

import copy
import csv
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I leaves the script's directory off sys.path
import score  # noqa: E402

BACKENDS = ['vm', 'dart2js', 'dart2wasm']


class TargetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.target = json.loads(score.TARGET.read_text())
        self.in_scope = {r['cap_id']: r for r in csv.DictReader(score.MANIFEST.read_text().splitlines(), delimiter='\t')
                         if r['in_scope'] == 'yes'}
        self.rules = json.loads(score.RULES.read_text())['rows']
        self.cases = score.case_index()

    def statuses(self, status: str = 'PRESENT') -> dict:
        return {cid: {b: (status, 'measured') for b in BACKENDS} for cid in self.target['rows']}

    def test_committed_file_is_consistent(self) -> None:
        self.assertEqual(score.target_problems(self.target, self.in_scope), [])
        self.assertEqual(len(self.target['rows']) + len(self.target['excluded']), len(self.in_scope))

    def test_area_clauses_hold_as_the_rule_states_them(self) -> None:
        owner = {'filesystem': 'A1', 'telemetry': 'A2', 'deployment': 'A3', 'runtime_cli': 'A4'}
        for cid, row in self.in_scope.items():
            if row['area'] in owner:
                self.assertEqual(self.target['excluded'][cid]['clause'], owner[row['area']], cid)
            elif cid in self.target['excluded']:
                self.assertNotIn(self.target['excluded'][cid]['clause'], owner.values(), cid)

    def test_mount_clause_is_every_case_needing_a_filesystem(self) -> None:
        for cid, row in self.in_scope.items():
            if row['area'] != 'os_calls':
                continue
            cases = self.rules.get(cid, {}).get('cases') or []
            mount = bool(cases) and all('fs' in self.cases[c] for c in cases)
            self.assertEqual(self.target['excluded'].get(cid, {}).get('clause') == 'B1', mount, cid)

    def test_upstream_clause_needs_no_binding_and_no_oracle_surface(self) -> None:
        for cid, ex in self.target['excluded'].items():
            if ex['clause'] != 'C1':
                continue
            row, rule = self.in_scope[cid], self.rules.get(cid, {})
            self.assertEqual((row['python_cite'], row['js_cite']), ('-', '-'), cid)
            # No python-binding oracle: no rule at all, or upstream's Rust API as the oracle; or a reviewed call.
            surface = any(rule.get(k) for k in ('cases', 'probes', 'worker', 'session')) and rule.get('oracle') != 'ref-rust'
            self.assertTrue(not surface or 'judgement' in ex, cid)

    def test_allowed_entries_are_the_designs(self) -> None:
        for cid, row in self.target['rows'].items():
            allowed = row.get('allowed')
            if allowed:
                self.assertIn(allowed['kind'], score.ALLOWED_KINDS, cid)
                self.assertTrue(allowed['reason'] and allowed['cite'], cid)

    def test_all_present_passes_and_a_stale_allowed_entry_fails(self) -> None:
        lines, fails = score.score_target(self.target, self.statuses(), BACKENDS)
        self.assertEqual(fails, 0)
        t = copy.deepcopy(self.target)
        cid = next(c for c, r in t['rows'].items() if len(r['backends']) == 3)
        t['rows'][cid]['allowed'] = {'kind': 'owner-exception', 'status': 'N/A', 'backends': BACKENDS,
                                     'reason': 'r', 'cite': 'c'}
        self.assertEqual(score.target_problems(t, self.in_scope), [])
        lines, fails = score.score_target(t, self.statuses(), BACKENDS)
        self.assertEqual(fails, 3)  # PRESENT on every backend it is allowed on: stale
        self.assertIn('PRESENT STALE', '\n'.join(lines))
        st = self.statuses()
        st[cid] = {b: ('N/A', 'recorded') for b in BACKENDS}
        _, fails = score.score_target(t, st, BACKENDS)
        self.assertEqual(fails, 0)  # the status the entry accepts
        st[cid]['vm'] = ('ABSENT', 'recorded')
        _, fails = score.score_target(t, st, BACKENDS)
        self.assertEqual(fails, 1)  # any other status still fails

    def test_a_forced_mismatch_is_shown_and_counted(self) -> None:
        st = self.statuses()
        cid = next(c for c, r in self.target['rows'].items() if not r.get('allowed') and len(r['backends']) == 3)
        st[cid]['dart2js'] = ('PARTIAL', 'measured')
        lines, fails = score.score_target(self.target, st, BACKENDS)
        self.assertEqual(fails, 1)
        self.assertIn(f'| {cid} |', '\n'.join(lines))
        self.assertIn('PARTIAL FAIL', '\n'.join(lines))

    def test_a_backend_a_row_is_not_a_target_on_is_not_scored(self) -> None:
        cid = next(c for c, r in self.target['rows'].items() if r['backends'] == ['vm'])
        st = self.statuses()
        st[cid]['dart2js'] = st[cid]['dart2wasm'] = ('REFUSED', 'recorded')
        _, fails = score.score_target(self.target, st, BACKENDS)
        self.assertEqual(fails, 0)

    def test_a_dropped_backend_is_named_with_its_clause(self) -> None:
        cid = next(c for c, r in self.target['rows'].items() if r['backends'] == ['vm'])
        self.assertEqual(self.target['rows'][cid]['backends_excluded'], {'dart2js': 'G7', 'dart2wasm': 'G7'})
        t = copy.deepcopy(self.target)
        del t['rows'][cid]['backends_excluded']['dart2wasm']
        t['rows'][cid]['backends_excluded']['dart2js'] = 'G9'
        problems = '\n'.join(score.target_problems(t, self.in_scope))
        self.assertIn('must split', problems)
        self.assertIn("'G9', which is not in `clauses`", problems)

    def test_a_malformed_file_is_reported(self) -> None:
        t = copy.deepcopy(self.target)
        cid = next(iter(t['rows']))
        t['excluded'][cid] = {'area': t['rows'][cid]['area'], 'clause': 'A1', 'owner': 'M6'}
        dropped = next(c for c in t['rows'] if c != cid)
        del t['rows'][dropped]
        withallowed = next(c for c in t['rows'] if c != cid)
        t['rows'][withallowed]['allowed'] = {'kind': 'because', 'status': 'ABSENT', 'backends': ['vm']}
        problems = '\n'.join(score.target_problems(t, self.in_scope))
        self.assertIn(f'{cid} is both a target row and excluded', problems)
        self.assertIn(f'{dropped}: an in-scope manifest row that is neither', problems)
        self.assertIn("kind 'because'", problems)
        self.assertIn('needs a reason and a cite', problems)
        for backends in (None, []):
            t['rows'][withallowed]['allowed'] = {'kind': 'owner-exception', 'status': 'N/A', 'reason': 'r', 'cite': 'c'}
            if backends is not None:
                t['rows'][withallowed]['allowed']['backends'] = backends
            self.assertIn('non-empty list of backends', '\n'.join(score.target_problems(t, self.in_scope)))

    def test_the_target_flags_are_not_silently_skipped(self) -> None:
        import contextlib  # noqa: PLC0415
        import io  # noqa: PLC0415
        from unittest import mock  # noqa: PLC0415

        for argv in (['score.py', '/nonexistent', '--assert-target'],
                     ['score.py', '/nonexistent', '--assert-gate', '--target', '/nonexistent/m3-target.json'],
                     ['score.py', '/nonexistent', '--assert-e0', '--target', '--assert-target'],
                     ['score.py', '/nonexistent', '--assert-worker', '--target']):
            with self.subTest(argv=argv), mock.patch.object(sys, 'argv', argv), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                score.main()
            self.assertNotEqual(cm.exception.code, 0)


if __name__ == '__main__':
    unittest.main()
