#!/usr/bin/env python3
"""Self-test of the oracle's canonical encoding: shared objects in sets encode one way per graph.

usage: python -I tool/parity/oracle/test_canon.py   (with the pinned pydantic-monty installed)

Each graph is built afresh many times, so the objects' ids and the sets' iteration orders vary;
its encoding must not, or the oracle refuses the graph.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I leaves the script's directory off sys.path
import run_cases  # noqa: E402

RUNS = 500


def nan_tuples(n: int) -> list[tuple[float]]:
    return [(float('nan'),) for _ in range(n)]


class SetEncoding(unittest.TestCase):
    def encodings(self, build) -> set[str]:
        return {json.dumps(run_cases.canon(build())) for _ in range(RUNS)}

    def test_stable(self):
        graphs = {
            'shared outside, then a set': lambda: (lambda a, b: [a, {a, b}])(*nan_tuples(2)),
            'a set, then shared outside': lambda: (lambda a, b: [{a, b}, a, b])(*nan_tuples(2)),
            'members holding shared tuples': lambda: (lambda a, b: [a, b, {(a,), (b,)}])(*nan_tuples(2)),
            'nested sets of numbered tuples': lambda: (
                lambda a, b: [a, b, {frozenset({(a, b)}), frozenset({(b, a)})}])(*nan_tuples(2)),
            'ints': lambda: [{3, 1, 2}, frozenset({'b', 'a'})],
        }
        for name, build in graphs.items():
            with self.subTest(graph=name):
                self.assertEqual(len(self.encodings(build)), 1)

    def test_nested_sets_encode_in_bounded_time(self):
        import time  # noqa: PLC0415
        x: object = 0
        for _ in range(40):
            x = frozenset([x])
        t = time.monotonic()
        run_cases.canon(x)
        self.assertLess(time.monotonic() - t, 2.0)

    def test_refused_when_ties_would_be_numbered_in_iteration_order(self):
        graphs = {
            'tied members numbered only inside sets': lambda: (lambda a, b, x: [{a, b}, {a}, {b, x}])(
                *nan_tuples(2), (1,)),
            'tied nested sets': lambda: (lambda a, b, c: {frozenset({(a, b)}), frozenset({(b, a)}),
                                                          frozenset({(a, c)})})(*nan_tuples(3)),
        }
        for name, build in graphs.items():
            with self.subTest(graph=name), self.assertRaises(RuntimeError):
                run_cases.canon(build())


if __name__ == '__main__':
    unittest.main(verbosity=1)
