#!/usr/bin/env python3
"""Self-test of the worker engine's comparison (worker_compare.py), offline.

usage: python3 -I tool/parity/test_worker_compare.py

Over every committed expected outcome (tool/parity/expected/*/ref-py.json):
  - it matches itself, and a relabelled copy of itself (reference ids renumbered, set members
    reversed, sandbox ids renamed);
  - every negative control that finds something to act on in it turns the comparison into a
    difference.
Plus constructed graphs for the cases the committed outcomes may not contain: tuples of distinct
NaNs shared in and out of a set, a uuid inside a string, a host-side input refusal.
"""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I leaves the script's directory off sys.path
import worker_compare as wc  # noqa: E402

EXPECTED = Path(__file__).resolve().parent / 'expected'
MUTATIONS = ['copy-shared-root', 'copy-shared-namedtuple', 'copy-shared-proxy', 'copy-shared-attribute-child',
             'copy-shared-call-arg', 'retarget-ref', 'string-digits', 'string-uuid', 'merge-instance-ids',
             'change-instance-id', 'drop-zone-name', 'empty-zone-name-to-none']


def outcomes() -> list[tuple[str, dict]]:
    out = []
    for p in sorted(EXPECTED.glob('*/ref-py.json')):
        o = json.loads(p.read_text())
        if o.get('steps') and not o.get('runner_error'):
            out.append((p.parent.name, o))
    return out


def relabel(o: dict) -> dict:
    """[o] with every reference id renumbered, set members reversed and sandbox ids renamed."""
    o = copy.deepcopy(o)
    names: dict[str, str] = {}

    def walk(x):
        if isinstance(x, list):
            for y in x:
                walk(y)
        elif isinstance(x, dict):
            if 'id' in x:
                x['id'] = 1000 - x['id']
            for k in wc.ID_FIELDS:
                if isinstance(x.get(k), str):
                    x[k] = names.setdefault(x[k], f'renamed-{len(names)}')
            if x.get('t') in ('set', 'frozenset'):
                x['items'].reverse()
            for y in x.values():
                walk(y)

    walk(o['steps'])
    return o


def f(bits: str) -> dict:
    return {'t': 'float', 'v': 'nan', 'bits': bits}


def outcome(*values, **step) -> dict:
    return {'steps': [{'value': v, 'exception': None, 'prints': [], 'os_calls': [], 'ext_calls': [],
                       'host_calls': [], **step} for v in values]}


class RealOutcomes(unittest.TestCase):
    def test_each_matches_itself_and_a_relabelled_copy(self):
        n = 0
        for cid, o in outcomes():
            with self.subTest(case=cid):
                self.assertEqual(wc.compare(o, copy.deepcopy(o)), [])
                self.assertEqual(wc.compare(o, relabel(o)), [])
                n += 1
        self.assertGreater(n, 500)

    def test_every_applicable_control_is_caught(self):
        hits = dict.fromkeys(MUTATIONS, 0)
        for cid, o in outcomes():
            for m in MUTATIONS:
                got = copy.deepcopy(o)
                if wc.mutate(got, m):
                    hits[m] += 1
                    with self.subTest(case=cid, mutation=m):
                        self.assertNotEqual(wc.compare(o, got), [])
        # The committed outcomes exercise these controls at least once each.
        for m in ('copy-shared-root', 'copy-shared-proxy', 'string-digits', 'retarget-ref'):
            self.assertGreater(hits[m], 0, m)


class Constructed(unittest.TestCase):
    # [{a, b}, a, b] with a = (nan1,), b = (nan2,): members tie under any ordering.
    a = {'id': 1, 't': 'tuple', 'items': [f('7ff8000000000000')]}
    b = {'id': 2, 't': 'tuple', 'items': [f('7ff8000000000000')]}
    nan_graph = {'t': 'list', 'items': [{'t': 'set', 'items': [a, b]}, {'t': 'ref', 'id': 1}, {'t': 'ref', 'id': 2}]}

    def test_nan_tuples_match_in_either_set_order(self):
        other = copy.deepcopy(self.nan_graph)
        other['items'][0]['items'].reverse()
        self.assertEqual(wc.compare(outcome(self.nan_graph), outcome(other)), [])

    def test_nan_tuples_a_changed_external_reference_fails(self):
        other = copy.deepcopy(self.nan_graph)
        other['items'][1]['id'] = 2
        self.assertEqual(wc.compare(outcome(self.nan_graph), outcome(other)), ['V'])

    def test_a_copied_shared_root_fails(self):
        other = copy.deepcopy(self.nan_graph)
        other['items'][1] = {'t': 'tuple', 'items': [f('7ff8000000000000')]}
        self.assertEqual(wc.compare(outcome(self.nan_graph), outcome(other)), ['V'])

    def test_a_wide_symmetric_set_matches_without_exhausting_the_search(self):
        # [{a0..a8}, a0..a8], each ai a distinct tuple holding the same NaN bits, against the same
        # graph with the set's members reversed: every member ties with every other.
        members = [{'id': i, 't': 'tuple', 'items': [f('7ff8000000000000')]} for i in range(1, 10)]
        ref = {'t': 'list', 'items': [{'t': 'set', 'items': members}]
               + [{'t': 'ref', 'id': i} for i in range(1, 10)]}
        got = copy.deepcopy(ref)
        got['items'][0]['items'].reverse()
        self.assertEqual(wc.compare(outcome(ref), outcome(got)), [])
        got['items'][1], got['items'][2] = got['items'][2], got['items'][1]
        self.assertEqual(wc.compare(outcome(ref), outcome(got)), [])  # still one relabelling
        got['items'][2] = {'t': 'ref', 'id': 9}
        self.assertEqual(wc.compare(outcome(ref), outcome(got)), ['V'])

    def test_a_large_set_matches_in_either_order_in_bounded_memory(self):
        import tracemalloc  # noqa: PLC0415
        ints = [{'t': 'int', 'v': str(i)} for i in range(450)]
        ref = {'t': 'set', 'items': ints}
        for got in ({'t': 'set', 'items': list(ints)}, {'t': 'set', 'items': list(reversed(ints))}):
            tracemalloc.start()
            self.assertEqual(wc.compare(outcome(ref), outcome(got)), [])
            peak = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
            self.assertLess(peak, 50 * 2**20)

    def test_core_scoring_keeps_ordinary_keys_named_like_id_fields(self):
        import score  # noqa: PLC0415 (local module, same dir)
        call = {'t': 'dict', 'items': [], 'instance_id': 'x'}
        self.assertIn('instance_id', score.norm(call))
        self.assertEqual(score.norm({'kwargs': {'instance_id': {'t': 'int', 'v': '1'}}}),
                         {'kwargs': {'instance_id': {'t': 'int', 'v': '1'}}})
        self.assertNotIn('instance_id', score.norm({'t': 'proxy', 'instance_id': 'u'}))

    def test_an_oracle_outcome_with_rewritten_strings_is_no_oracle(self):
        import tempfile  # noqa: PLC0415

        import score  # noqa: PLC0415 (local module, same dir)
        with tempfile.TemporaryDirectory() as d:
            for side, name in (('exp', 'ref-py.json'), ('out', 'worker-vm.json')):
                (Path(d) / side / 'c').mkdir(parents=True)
                o = outcome({'t': 'str', 'v': '<uuid>'})
                if side == 'exp':
                    o['strings_rewritten'] = True
                (Path(d) / side / 'c' / name).write_text(json.dumps(o))
            self.assertEqual(score.classify_worker('c', 'vm', Path(d) / 'out', Path(d) / 'exp'),
                             ('NO-ORACLE', ['strings_rewritten']))

    def test_nested_sets_ordered_differently_by_each_side_match(self):
        # The oracle orders frozenset members by their text with bits, the runner by display text.
        lo = {'t': 'float', 'v': '-1.5', 'bits': 'bff8000000000000'}
        hi = {'t': 'float', 'v': '2.5', 'bits': '4004000000000000'}
        ref = {'t': 'set', 'items': [{'t': 'frozenset', 'items': [hi, lo]}]}
        got = {'t': 'set', 'items': [{'t': 'frozenset', 'items': [lo, hi]}]}
        self.assertEqual(wc.compare(outcome(ref), outcome(got)), [])
        got['items'][0]['items'][0] = dict(lo, bits='bff8000000000001')
        self.assertEqual(wc.compare(outcome(ref), outcome(got)), ['V'])

    def test_uuid_text_in_a_string_is_compared(self):
        s = '12345678-1234-4234-8234-123456789abc'
        ref = outcome({'t': 'str', 'v': s})
        got = copy.deepcopy(ref)
        self.assertTrue(wc.mutate(got, 'string-uuid'))
        self.assertEqual(wc.compare(ref, got), ['V'])

    def test_sandbox_ids_hold_across_steps(self):
        p = {'t': 'proxy', 'class': 'P', 'dataclass': True, 'instance_id': 'id#1', 'attrs': []}
        ref = outcome(p, p)
        got = outcome(dict(p, instance_id='u-1'), dict(p, instance_id='u-1'))
        self.assertEqual(wc.compare(ref, got), [])
        got['steps'][1]['value']['instance_id'] = 'u-2'
        self.assertEqual(wc.compare(ref, got), ['V'])

    def test_a_host_input_refusal_matches_only_a_local_refusal(self):
        ref = outcome(None, exception={'type': 'HOST:TypeError', 'message': "unhashable type: 'list'",
                                       'traceback': []})
        got = outcome(None, exception={'type': 'ArgumentError', 'message': 'x', 'traceback': []},
                      thrown='ArgumentError')
        self.assertEqual(wc.compare(ref, got, local_refusal=True), [])
        self.assertNotEqual(wc.compare(ref, got), [])  # not a local_refusal case
        ran = outcome({'t': 'none'})
        self.assertNotEqual(wc.compare(ref, ran, local_refusal=True), [])


if __name__ == '__main__':
    unittest.main(verbosity=1)
