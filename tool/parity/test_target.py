#!/usr/bin/env python3
"""Self-test of the M3 parity target (tool/parity/m3-target.json; score.py --target), offline.

usage: python3 -I tool/parity/test_target.py

  - the committed file partitions the 185 in-scope manifest rows into target rows and excluded rows;
  - every exclusion names a clause, and the area clauses and the mount clause hold as the rule states them;
  - an `allowed` entry is one of the three kinds the design allows, with a reason and a cite, and an `evidenced_by`
    (the core tests that evidence the row) is well formed; only PT1, TGT1, TGT2 and P-F1 entries are approved;
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
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I leaves the script's directory off sys.path
import score  # noqa: E402

BACKENDS = ['vm', 'dart2js', 'dart2wasm']


# The owner's TGT1 allowances (2026-10-15): row -> (kind, status, backends).
TGT1 = {
    'snapshot_trust': ('owner-exception', 'N/A', BACKENDS),
    'resource_accounting': ('owner-exception', 'N/A', BACKENDS),
    'resource_failure_disposal': ('owner-exception', 'N/A', BACKENDS),
    'exception_value_conversion': ('owner-exception', 'PARTIAL', BACKENDS),  # cells: w_exc_unicode_version_in
    'file_handle_value_conversion': ('browser-divergence', 'PARTIAL', ['dart2js', 'dart2wasm']),
    'worker_diagnostics': ('browser-divergence', 'ABSENT', ['dart2js', 'dart2wasm']),
}
# The owner's TGT2 allowance (2026-10-15), scoped to the worker engine's one recorded divergence.
TGT2 = {'unstable_graph_access': ('owner-exception', 'PARTIAL', BACKENDS, ['w_graph_cycle_ret'])}
# The owner's P-F1 F-1-window allowance (FAST1(4) delegated), until F-2 restores host mounts.
PF1 = {'os_environment': ('owner-exception', 'PARTIAL', ['dart2js', 'dart2wasm'], ['x21_fallback_nonfs'])}


class TargetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.target = json.loads(score.TARGET.read_text())
        self.in_scope = {r['cap_id']: r for r in csv.DictReader(score.MANIFEST.read_text().splitlines(), delimiter='\t')
                         if r['in_scope'] == 'yes'}
        self.rules = json.loads(score.RULES.read_text())['rows']
        self.cases = score.case_index()

    def statuses(self, status: str = 'PRESENT') -> dict:
        """Every row [status] on every backend, except a row with an `allowed` entry, which has the status it accepts."""
        def cell(row: dict, b: str) -> tuple[str, str]:
            allowed = row.get('allowed') or {}
            return (allowed['status'], 'recorded') if b in allowed.get('backends', []) else (status, 'measured')
        return {cid: {b: cell(row, b) for b in BACKENDS} for cid, row in self.target['rows'].items()}

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

    def test_pt1_allowed_entries(self) -> None:
        """PT1: J08's two N/A rows and J10's four protocol rows carry an owner-exception, citing PT1; J10's name core tests."""
        j08 = ['host_callback_authority', 'cpython_value_adapter']
        j10 = ['protocol_frame_io', 'protocol_worker_state_machine', 'wire_domain_conversion',
               'protocol_version_compatibility_check']
        for cid in j08 + j10:
            allowed = self.target['rows'][cid]['allowed']
            self.assertEqual((allowed['kind'], allowed['status'], allowed['backends']), ('owner-exception', 'N/A', BACKENDS), cid)
            self.assertIn('PT1', allowed['cite'], cid)
            self.assertEqual('evidenced_by' in allowed, cid in j10, cid)
        for cid in j10:
            paths = [e['path'] for e in self.target['rows'][cid]['allowed']['evidenced_by']]
            self.assertTrue(any(p.startswith('test/') for p in paths), cid)
        # No row but PT1's, TGT1's, TGT2's and P-F1's has an allowance, and none is still open.
        self.assertEqual(sorted(c for c, r in self.target['rows'].items() if r.get('allowed')),
                         sorted(j08 + j10 + list(TGT1) + list(TGT2) + list(PF1)))
        self.assertFalse([c for c, r in self.target['rows'].items() if r.get('open')])

    def test_tgt1_allowed_entries(self) -> None:
        """TGT1: three J01 rows N/A on every backend, the oracle's Unicode version PARTIAL, two browser divergences."""
        for cid, (kind, status, backends) in TGT1.items():
            allowed = self.target['rows'][cid]['allowed']
            self.assertEqual((allowed['kind'], allowed['status'], allowed['backends']), (kind, status, backends), cid)
            self.assertIn('TGT1', allowed['cite'], cid)
            self.assertEqual(self.target['rows'][cid]['backends'], BACKENDS, cid)  # still a target everywhere

    def test_tgt2_allowed_entry(self) -> None:
        """TGT2: unstable_graph_access is PARTIAL on every backend, scoped to w_graph_cycle_ret, and the public manual
        path that the oracle's refusal takes (sp_resume_unconvertible) is a cell of the row, so it must MATCH."""
        rules = json.loads(score.RULES.read_text())['rows']
        for cid, (kind, status, backends, cells) in TGT2.items():
            allowed = self.target['rows'][cid]['allowed']
            self.assertEqual((allowed['kind'], allowed['status'], allowed['backends'], allowed['cells']),
                             (kind, status, backends, cells), cid)
            self.assertIn('TGT2', allowed['cite'], cid)
            self.assertIn('w_graph_cycle_ret', rules[cid]['worker']['diverge'])
            self.assertIn('sp_resume_unconvertible', rules[cid]['session_also']['probes'])

    def test_pf1_allowed_entry_is_scoped_and_expires(self) -> None:
        """P-F1 accepts only the host-mount cell's gap; other failures and a PRESENT row fail the gate."""
        for cid, (kind, status, backends, cells) in PF1.items():
            row = self.target['rows'][cid]
            allowed = row['allowed']
            self.assertEqual((allowed['kind'], allowed['status'], allowed['backends'], allowed['cells']),
                             (kind, status, backends, cells), cid)
            self.assertEqual(row['backends'], BACKENDS)
            self.assertIn('tool/parity/EXCEPTIONS.md#p-f1-f-1-window-owner-exception', allowed['cite'])
            self.assertIn('b14_env', self.rules[cid]['cases'])
            self.assertIn(cells[0], self.rules[cid]['cases'])
            target = {'rows': {cid: row}}
            core = {cid: {b: {'cells': [{'case': 'b14_env', 'class': 'MATCH'},
                                       {'case': cells[0], 'class': 'UNSUPPORTED'}]} for b in backends}}
            st = {cid: {b: ('PARTIAL', 'measured') for b in backends}}
            misses = score.scope_misses(target, core, self.rules, backends, None, Path('/nonexistent'), self.cases)
            self.assertEqual(misses, {})
            self.assertEqual(score.score_target(target, st, backends, misses)[1], 0)
            for b in backends:
                core[cid][b]['cells'][0]['class'] = 'DIVERGE'
            misses = score.scope_misses(target, core, self.rules, backends, None, Path('/nonexistent'), self.cases)
            self.assertEqual(misses, {(cid, b): ['b14_env'] for b in backends})
            self.assertEqual(score.score_target(target, st, backends, misses)[1], len(backends))
            for b in backends:
                with self.subTest(backend=b):
                    present = copy.deepcopy(st)
                    present[cid][b] = ('PRESENT', 'measured')
                    lines, fails = score.score_target(target, present, backends)
                    self.assertEqual(fails, 1)
                    self.assertIn('PRESENT STALE', '\n'.join(lines))

    def test_pf2_environment_vm_is_present_without_stale_allowance(self) -> None:
        cid = 'os_environment'
        rule = self.rules[cid]
        # Model both cells matching; VM must accept PRESENT and reject a regression.
        classes = ['MATCH' for _ in rule['cases']]
        status = score.row_status(classes)
        self.assertEqual(status, 'PRESENT')
        lines, fails = score.score_target({'rows': {cid: self.target['rows'][cid]}},
                                         {cid: {'vm': (status, 'measured')}}, ['vm'])
        self.assertEqual(fails, 0, '\n'.join(lines))
        _, fails = score.score_target({'rows': {cid: self.target['rows'][cid]}},
                                      {cid: {'vm': ('PARTIAL', 'measured')}}, ['vm'])
        self.assertEqual(fails, 1)

    def test_evidenced_by_is_validated(self) -> None:
        t = copy.deepcopy(self.target)
        a = t['rows']['protocol_frame_io']['allowed']
        for bad in ([], [{'path': 'x'}], [{'path': '/etc/passwd', 'tests': ['t'], 'exercises': 'e'}],
                    [{'path': '../x', 'tests': ['t'], 'exercises': 'e'}],
                    [{'path': 'x', 'tests': [], 'exercises': 'e'}], [{'path': 'x', 'tests': ['t'], 'exercises': ''}], 'x'):
            a['evidenced_by'] = bad
            self.assertTrue(score.target_problems(t, self.in_scope), bad)

    def test_evidence_is_checked_against_a_core_checkout(self) -> None:
        import tempfile  # noqa: PLC0415

        t = copy.deepcopy(self.target)
        a = t['rows']['protocol_frame_io']['allowed']
        a['evidenced_by'] = [{'path': 'test/a_test.dart', 'tests': ["one title split", 'absent'], 'exercises': 'e'}]
        with tempfile.TemporaryDirectory() as d:
            core = Path(d)
            mine = lambda ps: [p for p in ps if p.startswith('protocol_frame_io:')]  # noqa: E731 (the other rows' files are absent too)
            self.assertIn('does not exist', '\n'.join(mine(score.evidence_core_problems(t, core))))
            (core / 'test').mkdir()
            (core / 'test/a_test.dart').write_text(
                "import 'absent.dart'; // absent\ntest('one title '\n    'split', () {});\ngroup(\"a \\\"quoted\\\" group\", () {});\n")
            problems = mine(score.evidence_core_problems(t, core))
            self.assertEqual(len(problems), 1)
            self.assertIn("'absent'", problems[0])
            # Text that declares no test (an import, a comment) is not a title; a group's title is.
            a['evidenced_by'][0]['tests'] = ['absent.dart']
            self.assertEqual(len(mine(score.evidence_core_problems(t, core))), 1)
            # A test commented out, in either comment form, declares nothing.
            (core / 'test/a_test.dart').write_text("// test('deleted evidence', () {});\n/* test('deleted evidence', () {}); */\n"
                                                    "test('kept // not a comment', () {});\n")
            a['evidenced_by'][0]['tests'] = ['deleted evidence']
            self.assertEqual(len(mine(score.evidence_core_problems(t, core))), 1)
            a['evidenced_by'][0]['tests'] = ['kept // not a comment']
            self.assertEqual(mine(score.evidence_core_problems(t, core)), [])
            (core / 'test/a_test.dart').write_text("group(\"a \\\"quoted\\\" group\", () {});\n")
            a['evidenced_by'][0]['tests'] = ['a "quoted" group']
            self.assertEqual(mine(score.evidence_core_problems(t, core)), [])

    def test_declared_titles_reads_dart_and_rust(self) -> None:
        dart = "test('a', () {});\n  testWidgets(\"b\" 'c', (t) async {});\nfinal x = test_helper('not');\n// test('in a comment is still read', () {})\n"
        self.assertEqual(score.declared_titles('t.dart', dart)[:2], ['a', 'bc'])
        # Nested block comments, raw and triple-quoted titles, and Rust comments.
        self.assertEqual(score.declared_titles('t.dart', "/* o /* i */ test('gone', () {}); */ test('kept', () {});"), ['kept'])
        self.assertEqual(score.declared_titles('t.dart', "test(r'raw // t', () {}); test('''m\nl''', () {});"), ['raw // t', 'm\nl'])
        self.assertEqual(score.declared_titles('t.rs', '// #[test] fn a() {}\n/* #[test] fn b() {} */\n#[test]\nfn c() {}'), ['c'])
        rust = '#[test]\nfn one() {}\n\n#[test]\n#[ignore]\nasync fn two() {}\nfn not_a_test() {}\n'
        self.assertEqual(score.declared_titles('t.rs', rust), ['one', 'two'])

    def test_an_allowed_entry_without_backends_is_still_rejected(self) -> None:
        t = copy.deepcopy(self.target)
        del t['rows']['protocol_frame_io']['allowed']['backends']
        self.assertIn('non-empty list of backends', '\n'.join(score.target_problems(t, self.in_scope)))

    def test_all_present_passes_and_a_stale_allowed_entry_fails(self) -> None:
        lines, fails = score.score_target(self.target, self.statuses(), BACKENDS)
        self.assertEqual(fails, 0)
        t = copy.deepcopy(self.target)
        cid = next(c for c, r in t['rows'].items() if len(r['backends']) == 3 and not r.get('allowed'))
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
        st[cid]['vm'] = ('ABSENT', 'measured')
        _, fails = score.score_target(t, st, BACKENDS)
        self.assertEqual(fails, 1)  # any other measured status still fails

    def test_an_na_entry_accepts_only_an_unmeasured_row(self) -> None:
        """N/A: nothing measures the row, so it accepts the recorded baseline status (`*`), whatever it is; once a
        cell measures the row, only PRESENT (stale) or the measured status N/A would do."""
        t = copy.deepcopy(self.target)
        cid = next(c for c, r in t['rows'].items() if len(r['backends']) == 3 and not r.get('allowed'))
        t['rows'][cid]['allowed'] = {'kind': 'owner-exception', 'status': 'N/A', 'backends': BACKENDS,
                                     'reason': 'r', 'cite': 'c'}
        st = self.statuses()
        st[cid] = {'vm': ('PARTIAL', 'recorded'), 'dart2js': ('ABSENT', 'recorded'), 'dart2wasm': ('ABSENT', 'recorded')}
        lines, fails = score.score_target(t, st, BACKENDS)
        self.assertEqual(fails, 0)
        self.assertIn(f'| {cid} | {t["rows"][cid]["area"]} | PARTIAL* allowed | ABSENT* allowed | ABSENT* allowed |', lines)
        st[cid]['dart2js'] = ('ABSENT', 'measured')
        _, fails = score.score_target(t, st, BACKENDS)
        self.assertEqual(fails, 1)
        # Any other status accepts only itself, recorded or measured.
        t['rows'][cid]['allowed']['status'] = 'ABSENT'
        _, fails = score.score_target(t, st, BACKENDS)
        self.assertEqual(fails, 1)  # vm PARTIAL*

    def test_an_allowed_entry_scoped_to_cells_needs_every_other_cell_to_match(self) -> None:
        """`cells` scopes an entry: the row passes as allowed only if each of its other cells MATCHes."""
        from unittest import mock  # noqa: PLC0415

        t = copy.deepcopy(self.target)
        cid = 'file_handle_value_conversion'
        self.assertEqual(t['rows'][cid]['allowed']['cells'], ['x8handle_pos2p53', 'x8handle_pos2p63m1'])
        core = {cid: {b: {'cells': [{'case': 'x8handle_pos5', 'class': 'MATCH'},
                                    {'case': 'x8handle_pos2p53', 'class': 'DIVERGE'},
                                    {'case': 'x8handle_pos2p63m1', 'class': 'DIVERGE'}]} for b in BACKENDS}}
        rules = {cid: {}}
        mine = lambda m: {k: v for k, v in m.items() if k[0] == cid}  # noqa: E731 (the other scoped rows have no cells here)
        with mock.patch.object(score, 'engine_cells', lambda *_a, **_k: [{'case': 'w_fh_in', 'class': 'MATCH'}]):
            misses = mine(score.scope_misses(t, core, rules, BACKENDS, Path('/nonexistent'), Path('/nonexistent'), {}))
        self.assertEqual(misses, {})
        st = self.statuses()
        st[cid] = {b: ('PARTIAL', 'measured') for b in BACKENDS}
        _, fails = score.score_target(t, st, BACKENDS, misses)
        self.assertEqual(fails, 1)  # vm only: the entry is for the web
        # The negative control: one more diverging cell outside the scope (x8hostmode_neg) fails the entry.
        for b in BACKENDS:
            core[cid][b]['cells'].append({'case': 'x8hostmode_neg', 'class': 'DIVERGE'})
        with mock.patch.object(score, 'engine_cells', lambda *_a, **_k: [{'case': 'w_fh_in', 'class': 'MATCH'}]):
            misses = mine(score.scope_misses(t, core, rules, BACKENDS, Path('/nonexistent'), Path('/nonexistent'), {}))
        self.assertEqual(misses, {(cid, 'dart2js'): ['x8hostmode_neg'], (cid, 'dart2wasm'): ['x8hostmode_neg']})
        lines, fails = score.score_target(t, st, BACKENDS, misses)
        self.assertEqual(fails, 3)
        self.assertIn('PARTIAL FAIL (outside the allowed cells: x8hostmode_neg)', '\n'.join(lines))
        # A cell outside the scope with no oracle outcome is no MATCH either: it fails the entry too.
        with mock.patch.object(score, 'engine_cells', lambda *_a, **_k: [{'case': 'w_fh_in', 'class': 'NO-ORACLE'}]):
            misses = mine(score.scope_misses(t, core, rules, BACKENDS, Path('/nonexistent'), Path('/nonexistent'), {}))
        self.assertEqual(misses, {(cid, b): ['w_fh_in', 'x8hostmode_neg'] for b in ('dart2js', 'dart2wasm')})
        # A scoped cell the row does not have is a miss too (a renamed case cannot widen the scope silently).
        core[cid]['dart2js']['cells'] = [c for c in core[cid]['dart2js']['cells'] if c['case'] != 'x8handle_pos2p63m1']
        with mock.patch.object(score, 'engine_cells', lambda *_a, **_k: []):
            misses = score.scope_misses(t, core, rules, BACKENDS, None, Path('/nonexistent'), {})
        self.assertIn('x8handle_pos2p63m1 (no such cell)', misses[(cid, 'dart2js')])
        # The shape: `cells`, when present, is a non-empty list of names.
        for bad in ([], 'x8handle_pos2p53', [1]):
            t['rows'][cid]['allowed']['cells'] = bad
            self.assertIn('`cells`', '\n'.join(score.target_problems(t, self.in_scope)), bad)

    def test_a_forced_mismatch_is_shown_and_counted(self) -> None:
        st = self.statuses()
        cid = next(c for c, r in self.target['rows'].items() if not r.get('allowed') and len(r['backends']) == 3)
        st[cid]['dart2js'] = ('PARTIAL', 'measured')
        lines, fails = score.score_target(self.target, st, BACKENDS)
        self.assertEqual(fails, 1)
        self.assertIn(f'| {cid} |', '\n'.join(lines))
        self.assertIn('PARTIAL FAIL', '\n'.join(lines))

    def test_a_backend_a_row_is_not_a_target_on_is_not_scored(self) -> None:
        # Backend exclusions remain supported even though R-P1 retires G7.
        t = copy.deepcopy(self.target)
        cid = 'allocator_memory_limit'
        t['rows'][cid]['backends'] = ['vm']
        t['rows'][cid]['backends_excluded'] = {'dart2js': 'C1', 'dart2wasm': 'C1'}
        st = self.statuses()
        st[cid]['dart2js'] = st[cid]['dart2wasm'] = ('REFUSED', 'recorded')
        _, fails = score.score_target(t, st, BACKENDS)
        self.assertEqual(fails, 0)

    def test_a_dropped_backend_is_named_with_its_clause(self) -> None:
        t = copy.deepcopy(self.target)
        cid = 'allocator_memory_limit'
        t['rows'][cid]['backends'] = ['vm']
        t['rows'][cid]['backends_excluded'] = {'dart2js': 'C1', 'dart2wasm': 'C1'}
        self.assertEqual(score.target_problems(t, self.in_scope), [])
        del t['rows'][cid]['backends_excluded']['dart2wasm']
        t['rows'][cid]['backends_excluded']['dart2js'] = 'G9'
        problems = '\n'.join(score.target_problems(t, self.in_scope))
        self.assertIn('must split', problems)
        self.assertIn("'G9', which is not in `clauses`", problems)

    def test_rp1_memory_rows_require_all_backends(self) -> None:
        self.assertNotIn('G7', self.target['clauses'])
        for cid in ('allocator_memory_limit', 'hard_memory_ceiling'):
            row = self.target['rows'][cid]
            self.assertEqual(row['backends'], BACKENDS)
            self.assertNotIn('backends_excluded', row)
            self.assertNotIn('allowed', row)
            st = self.statuses()
            for b in ('dart2js', 'dart2wasm'):
                st[cid][b] = ('PARTIAL', 'measured')
            _, fails = score.score_target(self.target, st, BACKENDS)
            self.assertEqual(fails, 2)
            self.assertEqual(self.rules[cid]['session_also']['probes'], ['sp_memory_headroom'])
        for cid in ('global_allocator', 'memory_limit_headroom', 'memory_baseline_accounting'):
            self.assertEqual(self.target['excluded'][cid]['clause'], 'C1')

    def test_rp1_probes_run_through_the_session_engine(self) -> None:
        import run  # noqa: PLC0415

        probes = {'sp_print_kill_after_print', 'sp_print_volume_under_limit', 'sp_memory_headroom'}
        self.assertTrue(probes <= run.gate_ids('probes'))
        self.assertEqual(set(self.rules['print_callback']['session_also']['probes']), probes - {'sp_memory_headroom'})

    def test_standalone_type_check_has_no_remaining_core_obligation(self) -> None:
        self.assertEqual(self.target['clauses']['D1'], {
            'text': "standalone type checking was removed at the core cutover (#253); upstream's binding has no check-only call",
            'owner': 'upstream',
        })
        self.assertEqual(self.target['excluded']['type_check_source']['clause'], 'D1')
        self.assertEqual(self.target['excluded']['type_check_source']['owner'], 'upstream')

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

    def test_engine_cells_count_toward_a_row(self) -> None:
        from unittest import mock  # noqa: PLC0415

        only_engine, both, untouched = list(self.target['rows'])[:3]
        statuses = {cid: {b: ('ABSENT', 'recorded') for b in BACKENDS} for cid in self.target['rows']}
        statuses[both] = {b: ('PRESENT', 'measured') for b in BACKENDS}
        core_rows = {both: {b: {'status': 'PRESENT', 'cells': [{'class': 'MATCH'}]} for b in BACKENDS}}
        engine = {only_engine: [{'class': 'MATCH'}, {'class': 'NO-ORACLE'}],
                  both: [{'class': 'MATCH'}, {'class': 'DIVERGE'}]}  # a recorded worker divergence still diverges
        rules = {cid: {'row': cid} for cid in self.target['rows']}
        with mock.patch.object(score, 'engine_cells', lambda rule, *_: engine.get(rule['row'], [])):
            merged = score.with_engines(self.target, statuses, core_rows, rules, BACKENDS,
                                        Path('/nonexistent'), Path('/nonexistent'))
        self.assertEqual(merged[only_engine], {b: ('PRESENT', 'measured') for b in BACKENDS})
        self.assertEqual(merged[both], {b: ('PARTIAL', 'measured') for b in BACKENDS})
        self.assertEqual(merged[untouched], statuses[untouched])  # nothing measures it: its recorded status stands
        self.assertEqual(statuses[both], {b: ('PRESENT', 'measured') for b in BACKENDS})  # the input is not changed

    def test_core_also_mappings_are_checked(self) -> None:
        self.assertEqual(score.core_also_problems(self.rules, self.cases, score.ROOT / 'tool/parity/expected'), [])
        rules = copy.deepcopy(self.rules)
        owned_probe = next(p for r in rules.values() for p in r.get('probes') or [])
        rules['assert_annotations']['core_also'] = {'cases': ['no_such_case', 's01_int_bigint_input'],
                                                    'probes': ['no_such_probe', owned_probe]}
        rules['run_blocking']['core_also'] = {'cases': []}
        problems = '\n'.join(score.core_also_problems(rules, self.cases, score.ROOT / 'tool/parity/expected'))
        self.assertIn('case no_such_case is no case the core run runs', problems)
        self.assertIn('case s01_int_bigint_input is no case the core run runs', problems)  # an e0 case
        self.assertIn('probe no_such_probe has no oracle outcome', problems)
        self.assertNotIn(owned_probe, problems)
        self.assertIn('run_blocking: `core_also` must map', problems)
        # A probe with an oracle outcome that no row's `probes` names is fine: `core_also` owns it.
        unowned = sorted(p.name for p in (score.ROOT / 'tool/parity/expected').glob('sp_*')
                         if not any(p.name in (r.get('probes') or []) for r in self.rules.values()))[0]
        rules['assert_annotations']['core_also'] = {'probes': [unowned]}
        self.assertEqual([p for p in score.core_also_problems(rules, self.cases, score.ROOT / 'tool/parity/expected')
                          if p.startswith('assert_annotations')], [])

    def test_the_core_run_runs_the_probes_core_also_owns(self) -> None:
        import tempfile  # noqa: PLC0415
        from unittest import mock  # noqa: PLC0415

        import run  # noqa: PLC0415

        rules = json.loads(score.RULES.read_text())
        rules['rows']['assert_annotations']['core_also'] = {'probes': ['sp_only_core_also']}
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'rows.json'
            f.write_text(json.dumps(rules))
            with mock.patch.object(run, 'RULES', f):
                probes = run.core_probes()
        self.assertIn('sp_only_core_also', probes)
        self.assertTrue({p for r in self.rules.values() for p in r.get('probes') or []} <= set(probes))

    def test_a_core_local_refusal_matches_only_a_host_refusal(self) -> None:
        """In a case marked `local_refusal`, a step the oracle's host refused (`thrown: host`, no traceback) matches a
        core step the Dart host refused (`thrown: host`), whatever the type and message; nothing else does."""
        import tempfile  # noqa: PLC0415

        cid = 'x8hostmode_neg'
        self.assertTrue(self.cases[cid].get('local_refusal'))
        ref = json.loads((score.ROOT / 'tool/parity/expected' / cid / 'ref-py.json').read_text())
        self.assertEqual(ref['steps'][0].get('thrown'), 'host')
        rule = {'compare': ['V', 'E']}
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            (out / cid).mkdir()

            def got(step: dict) -> tuple:
                (out / cid / 'core-vm.json').write_text(json.dumps({'steps': [step], 'unsupported': {}}))
                return score.classify_case(cid, 'vm', rule, out, score.ROOT / 'tool/parity/expected')[:2]

            host = {'value': None, 'thrown': 'host',
                    'exception': {'type': 'ArgumentError', 'message': 'other words', 'traceback': []}}
            self.assertEqual(got(host), ('MATCH', []))
            # Raised in the sandbox (a frame), or not thrown on the host: not a host refusal.
            frame = {'filename': '<python-input-0>', 'line': 1}
            self.assertEqual(got({**host, 'exception': {**host['exception'], 'traceback': [frame]}})[0], 'DIVERGE')
            self.assertEqual(got({**host, 'thrown': 'MontyRuntimeError'})[0], 'DIVERGE')
            self.assertEqual(got({**host, 'value': {'t': 'int', 'v': '1'}})[0], 'DIVERGE')
            # An oracle step with a traceback is no host refusal: nothing is normalized.
            ref_tb = copy.deepcopy(ref)
            ref_tb['steps'][0]['exception']['traceback'] = [frame]
            with mock.patch.object(score, 'load', lambda path: ref_tb if path.name == 'ref-py.json'
                                   else json.loads(path.read_text()) if path.exists() else None):
                self.assertEqual(got(host)[0], 'DIVERGE')
            # Unmarked, the same step diverges on type and message.
            with mock.patch.object(score, 'local_refusal_cases', frozenset):
                self.assertEqual(got(host)[0], 'DIVERGE')

    def test_the_target_flags_are_not_silently_skipped(self) -> None:
        import contextlib  # noqa: PLC0415
        import io  # noqa: PLC0415
        from unittest import mock  # noqa: PLC0415

        for argv in (['score.py', '/nonexistent', '--assert-target'],
                     ['score.py', '/nonexistent', '--assert-gate', '--target', '/nonexistent/m3-target.json'],
                     ['score.py', '/nonexistent', '--assert-e0', '--target', '--assert-target'],
                     ['score.py', '/nonexistent', '--assert-worker', '--target'],
                     ['score.py', '/nonexistent', '--target-engines', '/nonexistent']):
            with self.subTest(argv=argv), mock.patch.object(sys, 'argv', argv), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                score.main()
            self.assertNotEqual(cm.exception.code, 0)


if __name__ == '__main__':
    unittest.main()
