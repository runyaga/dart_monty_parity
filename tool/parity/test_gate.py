#!/usr/bin/env python3
"""Self-test of the C1 gate (score.py --assert-gate), offline.

usage: python3 -I tool/parity/test_gate.py

Against the committed gate manifest, rows.json and expected outcomes:
  - the committed manifest and rules are complete (gate_problems finds nothing);
  - outcomes copied from the oracle pass the gate, and the gate fails when a gated case or probe
    has no outcome, when it differs, or when a pending one has no outcome at all;
  - a pending probe that records `unimplemented` passes, and the same record on a landed row fails;
  - a probe whose only difference is the suspension position fails: the session engine tolerates
    no `no-suspension-position`;
  - a case that did not record every feed, or a probe that recorded nothing, fails whatever its stage;
  - a rule that drops a row's core case, names a missing case or carries a named exception, a session
    rule the manifest does not gate, a session probe no gated rule names, and a stage with no rows,
    are each reported.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I leaves the script's directory off sys.path
import score  # noqa: E402

HERE = Path(__file__).resolve().parent
EXPECTED = HERE / 'expected'
BACKENDS = ['vm', 'dart2js', 'dart2wasm']


def gated_ids(gate: dict, rows: dict, landed_only: bool) -> list[tuple[str, str]]:
    """(kind, id) of every case and probe of the gate's rows, or of its landed rows only."""
    out = []
    for row, stage in gate['rows'].items():
        if landed_only and stage not in gate['landed']:
            continue
        for kind in ('cases', 'probes'):
            out += [(kind, cid) for cid in rows[row]['session'].get(kind, [])]
    return out


class GateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = json.loads(score.GATE.read_text())
        self.rows = json.loads(score.RULES.read_text())['rows']
        self.tmp = Path(tempfile.mkdtemp(prefix='parity-gate-'))
        self.spec = self.tmp / 'gate.json'
        self.out = self.tmp / 'out'
        # Every case and probe answered exactly as the oracle did, on every backend.
        for kind, cid in gated_ids(self.gate, self.rows, landed_only=False):
            ref = json.loads((EXPECTED / cid / 'ref-py.json').read_text())
            for b in BACKENDS:
                self.write(cid, b, {**ref, 'engine': 'session', 'backend': b})

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def write(self, cid: str, backend: str, doc: dict) -> None:
        d = self.out / cid
        d.mkdir(parents=True, exist_ok=True)
        (d / f'session-{backend}.json').write_text(json.dumps(doc))

    def assert_gate(self, gate: dict | None = None) -> int:
        self.spec.write_text(json.dumps(gate or self.gate))
        with contextlib.redirect_stdout(io.StringIO()):
            return score.assert_gate(self.spec, self.out, EXPECTED, BACKENDS, None, None)

    def landed_probe(self) -> str:
        return next(cid for kind, cid in gated_ids(self.gate, self.rows, landed_only=True) if kind == 'probes')

    def test_committed_rules_are_complete(self) -> None:
        self.assertEqual(score.gate_problems(self.gate, self.rows, score.case_index(), EXPECTED), [])

    def test_oracle_outcomes_pass(self) -> None:
        self.assertEqual(self.assert_gate(), 0)

    def test_missing_landed_outcome_fails(self) -> None:
        for kind, cid in gated_ids(self.gate, self.rows, landed_only=True)[:2]:
            with self.subTest(cid=cid):
                f = self.out / cid / 'session-dart2wasm.json'
                saved = f.read_text()
                f.unlink()
                self.assertEqual(self.assert_gate(), 1)
                f.write_text(saved)

    def test_missing_pending_outcome_fails(self) -> None:
        kind, cid = next(x for x in gated_ids(self.gate, self.rows, landed_only=False)
                         if x not in gated_ids(self.gate, self.rows, landed_only=True))
        (self.out / cid / 'session-vm.json').unlink()
        self.assertEqual(self.assert_gate(), 1)

    def test_landed_divergence_fails(self) -> None:
        kind, cid = next(x for x in gated_ids(self.gate, self.rows, landed_only=True) if x[0] == 'cases')
        got = json.loads((self.out / cid / 'session-vm.json').read_text())
        got['steps'][-1]['value'] = {'t': 'str', 'v': 'not what upstream returned'}
        self.write(cid, 'vm', got)
        self.assertEqual(self.assert_gate(), 1)

    def test_unimplemented_is_pending_only(self) -> None:
        pid = self.landed_probe()
        for b in BACKENDS:
            self.write(pid, b, {'probe': pid, 'engine': 'session', 'backend': b, 'events': [],
                                'runner_error': None, 'unimplemented': 'not yet'})
        self.assertEqual(self.assert_gate(), 1)
        gate = copy.deepcopy(self.gate)
        gate['landed'] = []
        self.assertEqual(self.assert_gate(gate), 0)

    def test_unimplemented_with_a_runner_error_fails(self) -> None:
        kind, pid = next(x for x in gated_ids(self.gate, self.rows, landed_only=False)
                         if x[0] == 'probes' and x not in gated_ids(self.gate, self.rows, landed_only=True))
        self.write(pid, 'vm', {'probe': pid, 'engine': 'session', 'backend': 'vm', 'events': [],
                               'runner_error': 'worker processes left running', 'unimplemented': 'not yet'})
        self.assertEqual(self.assert_gate(), 1)

    def test_unimplemented_against_a_failed_oracle_fails(self) -> None:
        kind, pid = next(x for x in gated_ids(self.gate, self.rows, landed_only=False)
                         if x[0] == 'probes' and x not in gated_ids(self.gate, self.rows, landed_only=True))
        for b in BACKENDS:
            self.write(pid, b, {'probe': pid, 'engine': 'session', 'backend': b, 'events': [],
                                'runner_error': None, 'unimplemented': 'not yet'})
        expected = Path(tempfile.mkdtemp(prefix='parity-gate-expected-'))
        try:
            shutil.copytree(EXPECTED, expected, dirs_exist_ok=True)
            (expected / pid / 'ref-py.json').write_text(json.dumps({'probe': pid, 'events': [],
                                                                    'runner_error': 'oracle failed'}))
            self.spec.write_text(json.dumps(self.gate))
            with contextlib.redirect_stdout(io.StringIO()):
                rc = score.assert_gate(self.spec, self.out, expected, BACKENDS, None, None)
            self.assertEqual(rc, 1)
        finally:
            shutil.rmtree(expected)

    def test_position_is_compared(self) -> None:
        ref = json.loads((EXPECTED / 'k_function' / 'ref-py.json').read_text())
        got = copy.deepcopy(ref)
        for e in got['events']:
            if 'position' in e:
                e['position'] = None
        self.write('k_function', 'vm', got)
        self.assertEqual(score.classify_session_probe('k_function', 'vm', self.out, EXPECTED)[0], 'DIVERGE')
        # The core engine's rule tolerates exactly this difference; the session engine does not.
        from probes_compare import judge  # noqa: PLC0415 (local module, same dir)
        self.assertEqual(judge('k_function', ref, got, self.rows['run_progress_variants'])[0], 'MATCH')

    def pending(self, kind: str) -> str:
        return next(cid for k, cid in gated_ids(self.gate, self.rows, landed_only=False)
                    if k == kind and (k, cid) not in gated_ids(self.gate, self.rows, landed_only=True)
                    and json.loads((self.out / cid / 'session-vm.json').read_text()).get(
                        'steps' if kind == 'cases' else 'events'))

    def test_truncated_pending_case_fails(self) -> None:
        cid = self.pending('cases')
        got = json.loads((self.out / cid / 'session-vm.json').read_text())
        got['steps'] = got['steps'][:-1] if len(got['steps']) > 1 else []
        self.write(cid, 'vm', got)
        self.assertEqual(self.assert_gate(), 1)

    def test_empty_pending_probe_fails(self) -> None:
        pid = self.pending('probes')
        got = json.loads((self.out / pid / 'session-dart2js.json').read_text())
        got['events'] = []
        self.write(pid, 'dart2js', got)
        self.assertEqual(self.assert_gate(), 1)

    def test_incomplete_rules_are_reported(self) -> None:
        cases = score.case_index()
        rows = copy.deepcopy(self.rows)
        rows['os_sync_sleep']['session']['cases'].remove('b14_sleep_negative')
        rows['host_suspension_budget']['session']['cases'].append('no_such_case')
        rows['run_progress_variants']['session']['exceptions'] = rows['run_progress_variants']['exceptions']
        rows['os_entropy']['session'] = {'cases': ['b14_urandom']}
        rows['print_callback']['session']['probes'].remove('sp_print_raise_suspended')
        gate = copy.deepcopy(self.gate)
        gate['stages'].insert(1, 'C1-9')
        problems = '\n'.join(score.gate_problems(gate, rows, cases, EXPECTED))
        self.assertIn('b14_sleep_negative is a case of the row', problems)
        self.assertIn('no case file for no_such_case', problems)
        self.assertIn('tolerates no named exception', problems)
        self.assertIn('os_entropy: has a session rule but no stage', problems)
        self.assertIn('sp_print_raise_suspended: a session probe of the oracle but in no gated session rule', problems)
        self.assertIn("stage 'C1-9' gates no row", problems)


if __name__ == '__main__':
    unittest.main()
