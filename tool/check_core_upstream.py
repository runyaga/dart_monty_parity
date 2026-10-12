#!/usr/bin/env python3
"""Check that this repository's upstream pins equal the core checkout's.

tool/upstream holds copies of dart_monty_core's tool/upstream files: the
hash-pinned pydantic-monty lock and upstream pin the expected outcomes were
generated with, and the frozen capability manifest score.py scores against.
A core whose pins or manifest differ is measured against a different upstream
than these expected outcomes, so its parity numbers would mean nothing: update
this repository first (regenerate the expected outcomes, re-record the
baseline), then point core at the new commit.

usage: python3 -I tool/check_core_upstream.py --core DIR
Exit 0 when every file is byte-identical, 1 otherwise.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = ['requirements.lock', 'upstream-pin.json', 'capability-manifest.tsv', 'capability-manifest.json']


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--core', type=Path, required=True, help='dart_monty_core checkout')
    a = ap.parse_args()
    fails = []
    for f in FILES:
        ours, theirs = ROOT / 'tool/upstream' / f, a.core / 'tool/upstream' / f
        if not theirs.is_file():
            fails.append(f'{f}: missing from the core checkout')
        elif ours.read_bytes() != theirs.read_bytes():
            fails.append(f'{f}: differs from the core checkout')
    for f in fails:
        print(f'FAIL: tool/upstream/{f}')
    if fails:
        print('\nThe expected outcomes and baselines here were made for other upstream pins than this core has.')
        return 1
    print(f'PASS: {len(FILES)} upstream files equal the core checkout\'s')
    return 0


if __name__ == '__main__':
    sys.exit(main())
