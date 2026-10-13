#!/usr/bin/env python3
"""Self-test of the display suite's expected renderings (tool/parity/cases/display).

usage: python -I tool/parity/oracle/test_display.py [--rust BIN]   (with the pinned pydantic-monty installed)

1. Every display case's outcome carries upstream's rendered forms: one step that raised, its
   `display` block with display() in each format, str(), the exception() type and arguments, and
   display()'s refusal of an unknown format, all as strings (an oracle that stopped recording
   them would otherwise leave both sides of the comparison empty).
2. Negative control: in a copy of the tree, one expected rendering is changed by one character
   and the manifest re-bound to it (the hand edit check_parity_manifest.py cannot see). The
   copy's check_expected.py must then fail and name that outcome.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CASES = ROOT / 'tool/parity/cases/display'
EXPECTED = ROOT / 'tool/parity/expected'
FORMATS = ('traceback', 'type-msg', 'msg', 'str')
# The rendering the control changes: the collapse line of the 4-frame recursion.
CONTROL_CASE = 'd18_recursion_four_frames'
CONTROL_FROM = '[Previous line repeated 1 more times]'
CONTROL_TO = '[Previous line repeated 2 more times]'


def check_outcomes() -> list[str]:
    problems = []
    ids = sorted(p.stem for p in CASES.glob('*.json'))
    if not ids:
        return [f'no cases in {CASES}']
    for cid in ids:
        case = json.loads((CASES / f'{cid}.json').read_text())
        if not case.get('display') or not case.get('exact_strings'):
            problems.append(f'{cid}: a display case must set "display" and "exact_strings"')
        ref = EXPECTED / cid / 'ref-py.json'
        if not ref.exists():
            problems.append(f'{cid}: no expected outcome')
            continue
        out = json.loads(ref.read_text())
        steps = out.get('steps') or []
        if out.get('runner_error') or len(steps) != 1:
            problems.append(f'{cid}: expected one step and no runner error')
            continue
        disp = steps[0].get('display')
        if not isinstance(disp, dict):
            problems.append(f'{cid}: the step has no display block')
            continue
        for f in FORMATS:
            if not isinstance(disp.get(f), str):
                problems.append(f'{cid}: display[{f!r}] is not a string')
        exc = disp.get('exception') or {}
        if not isinstance(exc.get('type'), str) or (exc.get('args') or {}).get('t') != 'tuple':
            problems.append(f'{cid}: display["exception"] lacks its type or argument tuple')
        bad = disp.get('bad_format') or {}
        if not isinstance(bad.get('message'), str) or bad.get('type') != 'ValueError':
            problems.append(f'{cid}: display["bad_format"] lacks the ValueError refusal')
    return problems


def control(rust: Path | None) -> list[str]:
    with tempfile.TemporaryDirectory(prefix='parity-display-control-') as tmp:
        root = Path(tmp)
        shutil.copytree(ROOT / 'tool', root / 'tool')
        ref = root / 'tool/parity/expected' / CONTROL_CASE / 'ref-py.json'
        doc = json.loads(ref.read_text())
        disp = doc['steps'][0]['display']
        if CONTROL_FROM not in disp['traceback']:
            return [f'control: {CONTROL_CASE} has no {CONTROL_FROM!r} to change']
        disp['traceback'] = disp['traceback'].replace(CONTROL_FROM, CONTROL_TO)
        ref.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + '\n')
        # Re-bind the manifest to the edited outcome, so only the regeneration can catch it.
        bind = ('import json, sys; sys.path.insert(0, "tool"); import check_parity_manifest as m; '
                'm.MANIFEST.write_text(json.dumps(m.compute(), indent=1) + "\\n")')
        subprocess.run([sys.executable, '-I', '-c', bind], cwd=root, check=True)
        if subprocess.run([sys.executable, '-I', 'tool/check_parity_manifest.py'], cwd=root,
                          capture_output=True).returncode != 0:
            return ['control: the re-bound manifest does not pass check_parity_manifest.py']
        cmd = [sys.executable, '-I', 'tool/parity/oracle/check_expected.py', *(['--rust', str(rust)] if rust else [])]
        p = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
        named = f'FAIL: {CONTROL_CASE}/ref-py.json differs from the committed expected outcome'
        if p.returncode == 0 or named not in p.stdout:
            return [f'control: a wrong expected rendering of {CONTROL_CASE} was not caught '
                    f'(exit {p.returncode}):\n{p.stdout[-1500:]}{p.stderr[-1500:]}']
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--rust', type=Path, help='built parity-rust-oracle binary, passed to check_expected.py')
    a = ap.parse_args()
    problems = check_outcomes()
    problems += control(a.rust.resolve() if a.rust else None)
    for p in problems:
        print(f'FAIL: {p}')
    if problems:
        return 1
    print(f'PASS: {len(list(CASES.glob("*.json")))} display cases carry every rendered form; '
          f'a wrong expected rendering ({CONTROL_CASE}) fails the regeneration')
    return 0


if __name__ == '__main__':
    sys.exit(main())
