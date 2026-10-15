#!/usr/bin/env python3
"""Run the parity cases through dart_monty_core on one backend and collect outcomes.

usage:
  python3 -I tool/parity/run.py vm        --core DIR --out OUTDIR [--engine core|worker|session] [--cases GLOB ...]
  python3 -I tool/parity/run.py dart2js   --core DIR --out OUTDIR [--engine core|worker|session] [--cases GLOB ...]
  python3 -I tool/parity/run.py dart2wasm --core DIR --out OUTDIR [--engine core|worker|session] [--cases GLOB ...]

--core is the dart_monty_core checkout to measure (required). The runner
package (packages/monty_parity) is pointed at it with a pubspec_overrides.yaml
written for the run and removed afterwards, so the same runner measures any
core commit whose public API it compiles against (Monty / MontySession, core
0.110 on; parity's tag runner-0.23 holds the runner for the older API). Every
engine needs the worker executable that core's tool/build_worker.sh writes
into the core checkout on the VM, and core's committed worker assets
(lib/assets/dart_monty_core_worker.{js,wasm}) in the browser.

--engine core (the default) drives core's public Monty / MontySession through
the same driver as the session engine (packages/monty_parity/lib/src/
session_runner.dart; the two differ only in the scorer's rules) and writes
OUTDIR/<case>/core-<backend>.json. A case marked "isolate" runs in its own VM
process under a 60 s cap, so a run that never ends cannot hang the batch; a
case that hits the cap records `runner_error`. Its default cases are every
suite except the worker engine's own (e0 and worker_values) and the display
suite, whose expected renderings dart_monty_core's own tests check. The same
run then runs the core rows' probes (rows.json `probes`) and writes
OUTDIR/<probe>/core-<backend>.json.

--engine worker drives core's internal monty-proto worker (each case on a
fresh worker: a process on the VM, a Web Worker in the browser) and writes
OUTDIR/<case>/worker-<backend>.json. Its default cases are the e0 suite and
every case of a row's `worker` sets in rows.json (score.py --assert-worker). The
VM needs the worker executable that core's tool/build_worker.sh writes into
the core checkout. It exits 1 if the runner fails (its earlier outcomes for
the backend are removed first, so a failed run cannot reuse them). It then runs the worker crash tests on the same backend
(test/worker_crash_{vm,web}_test.dart) and exits 1 if they fail or run fewer
tests than expected; --no-crash-tests skips them.

--engine session drives core's public Monty / MontySession (Monty.checkout,
MontySession.feedRun): one pool for the run, its one worker Reset and reused
from case to case. It writes OUTDIR/<case>/session-<backend>.json; its
default cases are the e0 suite (score.py --assert-e0 --engine session) and
every case of a gated row's `session` rule (tool/parity/c1-gate.json,
rows.json; score.py --assert-gate). The same run then runs those rules'
probes (packages/monty_parity/lib/src/session_probes.dart), each probe
starting any other pool it configures, and writes
OUTDIR/<probe>/session-<backend>.json; --cases limits the run to cases and
runs no probe. Like the worker engine it needs core's worker executable on
the VM, and exits 1 if the runner fails. It runs no crash tests.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import platform
import re
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / 'packages/monty_parity'
CASES = ROOT / 'tool/parity/cases'
MARKER = 'PARITY_OUTCOME '
PROBE_MARKER = 'PARITY_PROBE '
ISOLATE_SECS = 60
ENGINES = ('core', 'worker', 'session')
# The worker and session engines' own suites; the core engine leaves them out by default.
WORKER_SUITE = 'e0'
WORKER_SUITES = (WORKER_SUITE, 'worker_values', 'session')
GATE = ROOT / 'tool/parity/c1-gate.json'
# Expected renderings (display() / str()) that core's renderer tests read; no engine runs them.
DISPLAY_SUITE = 'display'
RULES = ROOT / 'tool/parity/rows.json'
# Tests per backend in the worker crash test files (a floor: a filter or a
# load failure that runs fewer is a failure, not a pass).
CRASH_TESTS = {'vm': ('test/worker_crash_vm_test.dart', 2),
               'dart2js': ('test/worker_crash_web_test.dart', 3),
               'dart2wasm': ('test/worker_crash_web_test.dart', 3)}


def case_files(globs: list[str], engine: str) -> list[Path]:
    if globs:
        pats = globs
    elif engine == 'session':
        pats = [str(CASES / WORKER_SUITE / '*.json'),
                *(str(CASES / '*' / f'{cid}.json') for cid in sorted(gate_ids('cases')))]
    elif engine == 'worker':
        listed = {cid for rule in json.loads(RULES.read_text())['rows'].values()
                  for cases in (rule.get('worker') or {}).values() for cid in cases}
        pats = [str(CASES / WORKER_SUITE / '*.json'), *(str(CASES / '*' / f'{cid}.json') for cid in sorted(listed))]
    else:
        pats = [str(p) for p in sorted(CASES.glob('*/*.json')) if p.parent.name not in (*WORKER_SUITES, DISPLAY_SUITE)]
    files = sorted({Path(p).resolve() for g in pats for p in glob.glob(g)})
    if not files:
        sys.exit(f'no cases match {pats}')
    return files


def core_probes() -> list[str]:
    """Every probe of the core rows (rows.json `probes`)."""
    return sorted({p for rule in json.loads(RULES.read_text())['rows'].values() for p in rule.get('probes') or []})


def gate_ids(kind: str) -> set[str]:
    """Every case (or probe) of the `session` rules of the rows c1-gate.json gates."""
    rows = json.loads(RULES.read_text())['rows']
    return {cid for row in json.loads(GATE.read_text())['rows']
            for cid in (rows.get(row, {}).get('session') or {}).get(kind, [])}


def placeholder(cid: str, backend: str, why: str, engine: str = 'core') -> dict:
    return {'case': cid, 'engine': engine, 'backend': backend, 'steps': [], 'unsupported': {},
            'notes': {}, 'runner_error': why}


def write(out: Path, res: dict) -> None:
    d = out / res['case']
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{res['engine']}-{res['backend']}.json").write_text(json.dumps(res, indent=1, ensure_ascii=False) + '\n')


def sh(cmd: list[str], log: Path, timeout: int | None = None) -> int:
    with log.open('a') as f:
        f.write('$ ' + ' '.join(cmd) + '\n')
        f.flush()
        # Its own process group, so a timeout also kills what it started (worker processes).
        p = subprocess.Popen(cmd, cwd=PKG, stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()
            f.write(f'\n# killed after {timeout} s\n')
            return -999


def run_vm(cases: list[Path], out: Path, log: Path) -> None:
    batch = [c for c in cases if not json.loads(c.read_text()).get('isolate')]
    isolated = [c for c in cases if json.loads(c.read_text()).get('isolate')]
    runner = ['dart', 'run', 'bin/parity_session_vm.dart']
    if batch:
        rc = sh([*runner, str(out), *map(str, batch), '--engine=core'], log)
        print(f'vm batch: {len(batch)} cases, exit {rc}', flush=True)
    for c in isolated:
        cid = json.loads(c.read_text())['id']
        rc = sh([*runner, str(out), str(c), '--engine=core'], log, timeout=ISOLATE_SECS)
        if rc != 0:
            write(out, placeholder(cid, 'vm', f'parity_session_vm exit {rc} (-999 = killed after {ISOLATE_SECS} s)'))
        print(f'vm {cid}: exit {rc}', flush=True)
    for c in cases:
        cid = json.loads(c.read_text())['id']
        if not (out / cid / 'core-vm.json').exists():
            write(out, placeholder(cid, 'vm', 'no outcome written'))
    probes = core_probes()
    rc = sh([*runner, str(out), f'--probes={",".join(probes)}', '--engine=core'], log, timeout=600)
    print(f'vm probes: exit {rc}', flush=True)
    probe_placeholders(probes, out, 'core', 'vm', f'no outcome written (exit {rc})')


def bake(cases: list[Path], probes: list[str] = (), engine: str = 'session') -> None:
    lines = ['// GENERATED by tool/parity/run.py; not committed.', 'const parityCases = <String>[']
    for c in cases:
        lines.append('  ' + json.dumps(c.read_text()).replace('$', '\\$') + ',')
    lines.append('];')
    lines.append('const parityProbes = <String>[' + ''.join(f'{json.dumps(p)}, ' for p in probes) + '];')
    lines.append(f"const parityEngine = '{engine}';")
    (PKG / 'test/cases.g.dart').write_text('\n'.join(lines) + '\n')


def clear_outcomes(cases: list[Path], out: Path, engine: str, backend: str, probes: list[str] = ()) -> None:
    """Removes earlier outcomes, so a failed run cannot be scored on a previous run's."""
    for cid in [json.loads(c.read_text())['id'] for c in cases] + list(probes):
        (out / cid / f'{engine}-{backend}.json').unlink(missing_ok=True)


def probe_placeholders(probes: list[str], out: Path, engine: str, backend: str, why: str) -> None:
    """A runner error for each probe the run left without an outcome."""
    for pid in probes:
        f = out / pid / f'{engine}-{backend}.json'
        if not f.exists():
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({'probe': pid, 'engine': engine, 'backend': backend, 'events': [],
                                     'runner_error': why}, indent=1) + '\n')


def run_worker_vm(cases: list[Path], out: Path, log: Path, engine: str = 'worker', probes: list[str] = ()) -> int:
    extra = [f'--probes={",".join(probes)}'] if probes else []
    rc = sh(['dart', 'run', f'bin/parity_{engine}_vm.dart', str(out), *map(str, cases), *extra], log, timeout=1800)
    print(f'{engine} vm: {len(cases)} cases, {len(probes)} probes, exit {rc}', flush=True)
    for c in cases:
        cid = json.loads(c.read_text())['id']
        if not (out / cid / f'{engine}-vm.json').exists():
            write(out, placeholder(cid, 'vm', f'no outcome written (exit {rc})', engine))
    probe_placeholders(probes, out, engine, 'vm', f'no outcome written (exit {rc})')
    return rc


def web_test(backend: str, test_file: str, log: Path) -> int:
    """`dart test` [test_file] in Chrome on [backend]; the page loads core's worker assets from its package."""
    cmd = ['dart', 'test', '-p', 'chrome', '--reporter', 'expanded', test_file]
    if backend == 'dart2wasm':
        cmd[4:4] = ['-c', 'dart2wasm']
    return sh(cmd, log, timeout=3600)


def run_web(backend: str, cases: list[Path], out: Path, log: Path, core: Path, engine: str = 'core',
            probes: list[str] = ()) -> int:
    # Isolated cases are limit cases that may never finish where core cannot
    # bound them; the browser runs them too (it bounds nothing less than the VM).
    if engine == 'core':
        probes = core_probes()
        clear_outcomes(cases, out, engine, backend, probes)
    bake(cases, probes, engine)
    test_file = ('test/parity_worker_web_test.dart' if engine == 'worker'
                 else 'test/parity_session_web_test.dart')
    try:
        rc = web_test(backend, test_file, log)
    finally:
        (PKG / 'test/cases.g.dart').unlink(missing_ok=True)
    got = got_probes = 0
    for line in log.read_text(errors='replace').splitlines():
        i = line.find(MARKER)
        if i >= 0:
            res = json.loads(line[i + len(MARKER):])
            if res.get('backend') == backend and res.get('engine') == engine:
                write(out, res)
                got += 1
        i = line.find(PROBE_MARKER)
        if i >= 0:
            res = json.loads(line[i + len(PROBE_MARKER):])
            if res.get('backend') != backend or res.get('engine', 'core') != engine:
                continue
            d = out / res['probe']
            d.mkdir(parents=True, exist_ok=True)
            (d / f'{engine}-{backend}.json').write_text(json.dumps(res, indent=1, ensure_ascii=False) + '\n')
            got_probes += 1
    print(f'{backend} ({engine}): dart test exit {rc}, {got} case outcomes, {got_probes} probe outcomes', flush=True)
    for c in cases:
        cid = json.loads(c.read_text())['id']
        if not (out / cid / f'{engine}-{backend}.json').exists():
            write(out, placeholder(cid, backend, f'no outcome printed (dart test exit {rc})', engine))
    probe_placeholders(probes, out, engine, backend, f'no outcome printed (dart test exit {rc})')
    return rc


def run_crash_tests(backend: str, out: Path, core: Path) -> bool:
    """The worker crash tests on [backend]; true when every one ran and passed."""
    test_file, floor = CRASH_TESTS[backend]
    log = out / f'crash-{backend}.log'
    log.write_text('')
    if backend == 'vm':
        rc = sh(['dart', 'test', '-p', 'vm', '--reporter', 'expanded', test_file], log, timeout=600)
    else:
        rc = web_test(backend, test_file, log)
    text = log.read_text(errors='replace')
    # The expanded reporter's last line: `MM:SS +N[ ~S]: All tests passed!` (~S counts skips).
    ends = re.findall(r'\+(\d+)(?: ~(\d+))?: All tests passed!', text)
    passed = [int(n) for n, _ in ends]
    skipped = any(s for _, s in ends)
    ok = rc == 0 and bool(passed) and passed[-1] >= floor and not skipped
    rec = {'backend': backend, 'file': test_file, 'exit': rc, 'passed': passed[-1] if passed else 0,
           'floor': floor, 'skipped': skipped, 'ok': ok}
    (out / f'crash-{backend}.json').write_text(json.dumps(rec, indent=1) + '\n')
    print(f'{backend} worker crash tests: {"PASS" if ok else "FAIL"} {rec}', flush=True)
    return ok


def record_platform(out: Path) -> None:
    """Write OUTDIR/platform.json; refuse to mix outcomes from two architectures in one directory."""
    machine = platform.machine().lower()
    arch = {'amd64': 'x86_64', 'x86_64': 'x86_64', 'arm64': 'arm64', 'aarch64': 'arm64'}.get(machine, machine)
    rec = {'os': platform.system().lower(), 'arch': arch}
    p = out / 'platform.json'
    if p.exists() and json.loads(p.read_text())['arch'] != arch:
        sys.exit(f'{out} already holds outcomes measured on {json.loads(p.read_text())["arch"]}, not {arch}')
    p.write_text(json.dumps(rec) + '\n')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('backend', choices=['vm', 'dart2js', 'dart2wasm'])
    ap.add_argument('--core', type=Path, required=True, help='dart_monty_core checkout to measure')
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--engine', choices=ENGINES, default='core')
    ap.add_argument('--cases', nargs='*', default=[])
    ap.add_argument('--no-crash-tests', action='store_true', help='--engine worker: skip the crash tests')
    a = ap.parse_args()
    core = a.core.resolve()
    spec = core / 'pubspec.yaml'
    if 'name: dart_monty_core' not in (spec.read_text().splitlines() if spec.is_file() else []):
        sys.exit(f'--core {a.core}: not a dart_monty_core checkout (no pubspec.yaml naming dart_monty_core)')
    out = a.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    record_platform(out)
    log = out / (f'run-{a.backend}.log' if a.engine == 'core' else f'run-{a.engine}-{a.backend}.log')
    log.write_text('')
    cases = case_files(a.cases, a.engine)
    override = PKG / 'pubspec_overrides.yaml'
    override.write_text(f'dependency_overrides:\n  dart_monty_core:\n    path: {core}\n')
    try:
        if sh(['dart', 'pub', 'get'], log) != 0:
            sys.exit(f'dart pub get failed; see {log}')
        if a.engine in ('worker', 'session'):
            probes = sorted(gate_ids('probes')) if a.engine == 'session' and not a.cases else []
            clear_outcomes(cases, out, a.engine, a.backend, probes)
            if a.backend == 'vm':
                rc = run_worker_vm(cases, out, log, a.engine, probes)
            else:
                rc = run_web(a.backend, cases, out, log, core, a.engine, probes)
            crashes_ok = (a.engine != 'worker' or a.no_crash_tests
                          or run_crash_tests(a.backend, out, core))
            if rc != 0:
                print(f'FAIL: the {a.engine} runner exited {rc}; see {log}', flush=True)
            if rc != 0 or not crashes_ok:
                return 1
        elif a.backend == 'vm':
            run_vm(cases, out, log)
        else:
            run_web(a.backend, cases, out, log, core)
    finally:
        override.unlink(missing_ok=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
