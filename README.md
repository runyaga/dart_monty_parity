# dart_monty_parity

Measures how far [dart_monty_core](https://github.com/runyaga/dart_monty_core) matches upstream
[pydantic/monty](https://github.com/pydantic/monty) 1.1.0, per capability row of
`tool/upstream/capability-manifest.tsv`, on the Dart VM (a worker process), dart2js and dart2wasm (a Web
Worker).

The suite is self-contained: the cases, upstream's expected outcomes, the oracle that generates them,
the Dart runner, the scorer and the recorded baselines all live here. It runs against a dart_monty_core
checkout you name. dart_monty_core's CI checks this repository out at a pinned commit and asserts the
current baseline against every pull request.

## Compatible core

`CORE_REF` holds the dart_monty_core commit that `tool/parity/baseline-current.json` was recorded on;
CI's `current` job reproduces it there. A core commit that does not change parity behaviour passes too,
as long as its `tool/upstream` files equal the copies here (`tool/check_core_upstream.py`).

P-F1 requires the F-1 memory filesystem API (`OSAccess`, `MemoryFile`, unsafe
`CallbackFile`), pinned by `CORE_REF` to `58106a7c5807691b6fc2c82664229a3bd3680276`.
The P-F1 baseline records VM, dart2js and dart2wasm from the macOS arm64 host run
with real Chrome: 79, 78 and 78 PRESENT rows respectively out of 185.
Linux x86_64 applies the same architecture-independent F-1 window changes, with
the same PRESENT totals; a Linux measurement remains pending.

## Layout

| Path | What |
|---|---|
| `tool/parity/cases/<suite>/<id>.json` | parity cases: Python code or REPL feeds, typed inputs, scripted external / OS-call answers, limits, OS policy, mounts, host objects |
| `tool/parity/cases/display/<id>.json` | the display suite: errors whose expected outcome also records upstream's renderings (`display()` in each format, `str()`, the `exception()` type and arguments, the refusal of an unknown format). No engine runs them; dart_monty_core's traceback renderer tests read them (`tool/parity/oracle/test_display.py` checks they are recorded) |
| `tool/parity/expected/<id>/` | upstream's outcome for each case and probe: `ref-py.json` (pydantic-monty), `refmem-py.json` (upstream's in-memory filesystem), `ref-rust.json` (upstream monty's Rust API, three Rust-only probes) |
| `tool/parity/manifest.json` | binds every case (sha256) to the expected outcomes generated from it (sha256), plus the sha256 of the generator (the oracle's code) and of the upstream pins |
| `tool/parity/oracle/` | the reference oracle that generates `expected/`: `run_cases.py`, `run_probes.py`, `rust/` |
| `tool/parity/rows.json` | per capability row: its cases or probes, the status recorded at core 5873ec5 per backend, per-architecture expectations, and the named exceptions; and, for the M3 target only (`score.py --target-engines`), cases and probes another rule already runs that also measure the row: `core_also` (core-engine cases and probes; the core run runs every probe it names, so it may own a new probe no row's `probes` names), `session_also` (a gated row's session cases and probes) and the `worker` set `also`. The current baseline and the 5873ec5 instrument check do not read them |
| `tool/parity/baseline-current.json` | the current baseline: every case's result within every re-measured row, every row's status, per backend, the PRESENT totals, per-architecture differences and the list of intentional moves (`deltas`) |
| `tool/parity/e0.json` | the E0 gate: the `e0` cases core's monty-proto worker must match upstream on, per backend, and the cases deferred from it |
| `tool/parity/c1-gate.json`, `test_gate.py` | the C1 gate: per capability row, the PR whose landing makes the row's `session` rule (in `rows.json`) gated through the session engine, and which of those PRs have landed; and its offline self-test |
| `tool/parity/m3-target.json`, `test_target.py` | the M3 parity target: the in-scope manifest rows the M3 milestone owns, each with the backends it must be PRESENT on, the rest `excluded` under a named clause, and a row's `allowed` entry where an approved exception lets it be non-PRESENT; `score.py --target` prints it (not fatal until `--assert-target`), and with `--target-engines DIR` judges each row over the session and worker engines' outcomes in DIR too (CI job `target`); and its offline self-test |
| `tool/parity/EXCEPTIONS.md` | every tolerated difference (named exception) and rule choice, see below |
| `tool/parity/run.py`, `score.py`, `probes_compare.py` | run the cases through core on a backend; score each row against `expected/` |
| `tool/parity/worker_compare.py`, `test_worker_compare.py` | the worker engine's comparison (shared objects and sandbox ids up to relabelling, strings exactly) and its offline self-test |
| `packages/monty_parity` | the Dart runner: the `core` and `session` engines (one driver on core's public `Monty` / `MontySession`, `lib/src/session_runner.dart`, and its probes, `lib/src/session_probes.dart`; they differ in the scorer's rules) and the `worker` engine (core's internal monty-proto worker, `lib/src/worker_runner.dart`), VM and browser, plus the worker crash tests |
| `tool/upstream/` | copies of dart_monty_core's `tool/upstream`: the hash-pinned pydantic-monty lock, the upstream pin and the frozen capability manifest |
| `tool/check_parity_manifest.py` | offline check that `manifest.json` binds every case to its expected outcomes |
| `tool/check_core_upstream.py` | checks that a core checkout's `tool/upstream` equals the copies here |

## Running against a core checkout

Needs the Dart SDK 3.13.5 (the version every workflow pins; `dart format` output differs between
releases), Python 3.10+, Rust (core's `tool/build_worker.sh` builds the worker executable the VM backend
runs), and Chrome for dart2js and dart2wasm (they run core's committed worker assets,
`lib/assets/dart_monty_core_worker.{js,wasm}`).

```sh
git clone https://github.com/runyaga/dart_monty_core core
git -C core checkout "$(cat CORE_REF)"
bash core/tool/build_worker.sh

python3 -I tool/check_core_upstream.py --core core
python3 -I tool/parity/run.py vm        --core core --out /tmp/parity
python3 -I tool/parity/run.py dart2js   --core core --out /tmp/parity
python3 -I tool/parity/run.py dart2wasm --core core --out /tmp/parity
python3 -I tool/parity/score.py /tmp/parity --markdown /tmp/parity/score.md \
  --assert-current tool/parity/baseline-current.json
```

`--core` is required. `run.py` points the runner at it with a `pubspec_overrides.yaml` written for the
run and removed afterwards. `--cases GLOB ...` limits a run to some cases. One output directory holds one
architecture's outcomes; `score.py` reads the architecture from `platform.json` in it.

Every selected case marked `refmem: true` also runs independently through the public
session with `OSAccess(files: [MemoryFile(...)])` and writes
`OUTDIR/<case>/refmem-<backend>.json`. The VM, dart2js and dart2wasm all produce this
column. Stale memory outcomes are removed before the run. B1 rows and
`memory_filesystem` score it against `refmem-py.json`; until P-F2 this applies to the
VM too. The ordinary case outcome records `unsupported: fs.mounts` for mount
requests. Mount write and memory budget probes also remain unsupported until P-F2.
The case's `cwd` is passed to each feed, including manually resumed feeds.

`run.py` generates the temporary browser input `packages/monty_parity/test/cases.g.dart`.
After baking it, `run.py BACKEND --check --engine ENGINE [--cases GLOB ...]` checks
its bytes without launching Dart or a browser (no `--core` or `--out` needed).

### E0 through the worker

The `worker` engine drives core's internal monty-proto worker: a worker process on the VM, a Web Worker
running core's committed proto-worker wasm in the browser, each case on a fresh worker. It runs the `e0`
suite by default, then the worker crash tests on the same backend (a deadline kill, a native stack
overflow on the VM, a wasm trap under JSPI and the forced fallback in the browser; each must surface as a
typed `MontyCrashedError`, then a fresh worker must answer `1 + 1`). The VM needs core's worker
executable, built into the core checkout:

```sh
bash core/tool/build_worker.sh
for b in vm dart2js dart2wasm; do
  python3 -I tool/parity/run.py "$b" --engine worker --core core --out /tmp/e0
done
python3 -I tool/parity/score.py /tmp/e0 --assert-e0
```

`--assert-e0` fails unless every counted case of `tool/parity/e0.json` MATCHES on every backend.

The `session` engine runs the same E0 cases through core's public `Monty` / `MontySession`
(`Monty.checkout`, `MontySession.feedRun`). One pool serves the whole run, so its worker is Reset and
reused from case to case. The default `core` engine is the same driver
(`packages/monty_parity/lib/src/session_runner.dart`); the two differ only in the scorer's rules: `core`
outcomes are scored against `baseline-current.json`, whose rows may name an exception, `session` outcomes
against E0 and the C1 gate with none:

```sh
for b in vm dart2js dart2wasm; do
  python3 -I tool/parity/run.py "$b" --engine session --core core --out /tmp/e0
done
python3 -I tool/parity/score.py /tmp/e0 --assert-e0 --engine session
```
The same runs also carry the worker value rows' cases (each row's `worker` sets in `rows.json`; new cases
in `tool/parity/cases/worker_values`), asserted with `score.py /tmp/e0 --assert-worker`: every
`pre_cutover` case MATCHES, every recorded `diverge` case differs exactly as recorded, the `cutover`
cases are shown but must still run against an oracle,
and each negative control under `worker.controls` must turn a matching outcome into a difference. A case
under `deferred` is run and shown as DEFERRED but not counted; once it matches everywhere, the assertion
fails until it leaves `deferred`. The `core` engine leaves the `e0` suite out by default.

### The C1 gate through the session

The same session runs carry the C1 gate (`tool/parity/c1-gate.json`): the capability rows whose `session`
rule in `rows.json` names the cases and probes core's pool session must match upstream on. Each row is
assigned the C1 pull request that makes it pass (`stages`); a row is asserted once its stage is listed under
`landed`. Its cases are compared as E0's are; its probes (`lib/src/session_probes.dart` against
`oracle/run_probes.py`) as the core engine's are, but with no named exception, so a suspension's position is
compared. The pool, print and outbound-size probes (`sp_*`) and the `session` suite's cases (`ss_*`, exact
strings) were added for this gate.
Stages `C3-4`, `C3-5` and `C3-6` hold the C3 pull requests' parity rows (their cases come from the worker and
core suites and run here through the session), and `d1_calls` holds one case or probe per D1-calls rule
(`d1_rules` in the gate file: host failures during an external call, from expMGb's cells, upstream as the
oracle; rules 1, 2, 4 and 5 are `session` cases, 3, 6 and 7 probes). `C3-4` is landed (the session engine records an uncaught error's structured data: a Unicode error's
`encoding`, `object`, `start`, `end`, `reason`, a JSON error's `msg`, `doc`, `pos`, `lineno`, `colno`); `C3-5`,
`C3-6` and `d1_calls` are run and shown, not landed.

`C1-5` is landed: dump and load through the session (`sp_dump_*`, `sp_load_*`, `api_fork_session`, rows `dump_session`,
`load_session`, `snapshot_state_kinds`, `snapshot_errors`, `fork_session`). Idle and suspended dumps (a function, name,
OS-call or future suspension, from the snapshot or from the session while it waits), the freshness rule, the wrong kind
of dump, bytes the worker cannot load, the dump's own type-check setting and the suspension ceiling on a load, all
against upstream's outcomes.

`C1-6` is landed: host objects through the session (`sp_host_*`, nineteen probes of core's host-object cases on one host
class: eager and lazy attributes, methods, class values, construction, the conversion hook, identity, re-wrapping, the
id alias refusal and the empty registry after a load; and the `h_*` cases whose `host` block the session engine now
builds from core's wrappers: `host_world.dart`). Rows `host_eager_attributes`, `host_instance_value`,
`host_lazy_attributes`, `host_class_value`, `host_method_dispatch`, `host_constructor_policy`,
`host_value_conversion_hook`, `host_object_identity_lifecycle` and `snapshot_host_state_reconstruction`. The cases and
steps that cannot match, and why, are listed in `tool/parity/EXCEPTIONS.md`. `C5-v` is landed: `host_class_type_proxy`
(`h_b07`, `h_b08`, a sandbox class reached as a value after a restore, decoded as a `MontyClassTypeProxy` and sent
back), with core's value family. Identity `==` of core's wrappers is the named divergence `host-wrapper-identity-eq` there.

```sh
python3 -I tool/parity/score.py /tmp/e0 --assert-gate
```

`--assert-gate` fails unless every case and probe of a landed row MATCHES on every backend. A row of a later
stage is run and shown, not asserted; a probe the session cannot run yet records `unimplemented` (PENDING).
Whatever its stage, every case and probe must have an outcome on every backend and an oracle, and the rules
must be complete: a `session` rule covers each of its row's core cases and probes or says why not
(`not_run`), names only cases and probes that exist, carries no named exception, and is gated by the
manifest; every stage gates a row, and every `session` case and `sp_*` probe is in a gated rule. A behaviour
a stage's core pull request must have but that no upstream binding exposes comparably (a busy close, print
backpressure) is listed under `core_gated` and gated by core's own tests. A core change that makes a stage
pass moves it to `landed` here, with `CORE_REF`.

To reproduce the measured baseline instead, check out core `5873ec5` (where the capability manifest's
baseline was measured), run it with the runner of its API (this repository at the tag `runner-0.23`, the
last commit before the cut: `python3 -I <runner-0.23 checkout>/tool/parity/run.py ...`) and assert
`--assert-rows --assert-baseline` with this checkout's scorer. Expected under this suite's
rules: on arm64 FFI PRESENT 36/185, dart2js 35, dart2wasm 35; on x86_64 35/35/34 (see per-architecture
expectations in `EXCEPTIONS.md`). The manifest's frozen FFI number, 37, was scored more leniently;
`rows.json` `rescored` lists the rows that account for the difference. That core needs `npm ci` in its `js/` for the browser WASI worker, and on a toolchain
without its cross targets `RUSTUP_TOOLCHAIN=stable`.

Either historical assertion explicitly selects `runner-0.23`'s filesystem
measurement: `core-<backend>.json` against `ref-py.json` for B1 and
`memory_filesystem`. That runner writes no independent refmem outcomes. Current
scoring still requires `refmem-<backend>.json` against `refmem-py.json`, and a
missing file is `NO-OUTCOME`. Historical assertions cannot be combined with
`--assert-current`, `--write-current`, target or other engine scoring.

## Checks (CI)

- **`manifest`** (`ci.yaml`, every change): `tool/check_parity_manifest.py` fails if a case changed
  without its expected outcome being regenerated (or the reverse), an expected outcome changed outside a
  regeneration, or the oracle's code or the upstream pins changed since the outcomes were generated;
  then `tool/parity/test_worker_compare.py`, the worker comparison's self-test, and
  `tool/parity/test_gate.py`, the C1 gate's.
- **`current`** (`ci.yaml`): core at `CORE_REF`, all three backends, `--assert-current`; also analyses
  the runner and checks the upstream copies against that core.
- **`baseline`** (`ci.yaml`): core `5873ec5` through the `runner-0.23` runner, all three backends,
  `--assert-rows --assert-baseline`.
- **`e0`** (`ci.yaml`): core at `CORE_REF`, the `e0` suite and the worker value rows' cases through the
  worker engine on all three backends with the crash tests, `--assert-e0` and `--assert-worker`; then the
  `e0` suite and the C1 gate's cases and probes through the session engine on all three,
  `--assert-e0 --engine session` and `--assert-gate`.
- **`Parity oracle`** (`parity-oracle.yaml`): regenerates every expected outcome from the hash-pinned
  oracle and fails on any byte of difference; then `tool/parity/oracle/test_display.py` checks that every
  display case carries its renderings and that a deliberately wrong expected rendering fails the
  regeneration. Runs when a parity input changes (cases, expected outcomes, manifest, oracle code,
  upstream pins, the workflow), weekly, and on demand.

## Regenerating the expected outcomes

Needed after adding or editing a case, changing the oracle, or moving the upstream pin. Run on
**Linux x86_64** (what CI runs; one case's upstream error, `b11fs_unlink_dir`, follows the host errno and
differs on macOS):

```sh
python3.12 -m venv /tmp/oracle
/tmp/oracle/bin/pip install --require-hashes --only-binary :all: --no-deps \
  -r tool/upstream/requirements.lock \
  -r tool/parity/oracle/otel-requirements.lock
(cd tool/parity/oracle/rust && cargo build --locked --release)

# check: every committed outcome regenerates byte for byte, and the manifest binds them
/tmp/oracle/bin/python -I tool/parity/oracle/check_expected.py \
  --rust tool/parity/oracle/rust/target/release/parity-rust-oracle

# regenerate: rewrites expected/ and manifest.json in one step
/tmp/oracle/bin/python -I tool/parity/oracle/check_expected.py --write \
  --rust tool/parity/oracle/rust/target/release/parity-rust-oracle
```

Review the diff and commit the cases, `expected/` and `manifest.json` together. Moving the upstream pin
starts in dart_monty_core: copy its new `tool/upstream` files here, regenerate, and re-record the
baseline. The parity-only `tool/parity/oracle/otel-requirements.lock` pins the
telemetry SDK separately so the mirrored upstream lock stays identical to core.
Both locks are bound by the parity manifest.

## Updating the baseline

The baseline moves only on purpose. When a core change is meant to change parity (a case or a row
moves):

1. Here: run the suite against the core change on all three backends and re-record with
   `score.py OUT --write-current tool/parity/baseline-current.json`. `rows`, `cells`, `present` and
   `totals` are refreshed for the architecture in `OUT/platform.json`, including its
   `platforms` overrides. Other architectures' expectations and unselected backends are
   preserved, as are `subject` and `deltas`. All three backends are selected by default;
   a subset uses comma-separated `--backends vm,dart2js`. Re-run with `--assert-current`
   on the same artifacts to verify the recording. Add the move to `deltas`
   (row, backends, from, to, the core commit or PR, why), set `CORE_REF` to the core change's commit, and merge.
2. In dart_monty_core: bump the pinned commit of this repository in the same pull request as the change.
   Its `parity` job then asserts the new baseline.

## Named exceptions (`EXCEPTIONS.md`)

Every difference the scorer tolerates is a named exception in `tool/parity/rows.json`, with a stable ID.
`tool/parity/EXCEPTIONS.md` lists each one: the rows and cases it covers, the field, the observed
difference, why it is tolerated, the status the row would have without it, and when it is removed. It
also records the rule choices that decide statuses and the per-architecture expectations. Nothing that
is not listed there is tolerated.

## License

MIT, see `LICENSE`.

## R-P2 telemetry probes

`telemetry_plain`, `telemetry_print`, `telemetry_external`, `telemetry_manual`,
`telemetry_error` and `telemetry_dump_load` compare a recording `MontyTelemetry`
with Python's OpenTelemetry SDK span/log exporters and a raw recording meter.
Each runner installs once at process start, before any cases or probes. Each
telemetry scenario clears only its buffers and opens and closes a fresh pool
under an ambient host span. Other probes therefore also run instrumented.
The six scenarios explicitly use a 50 MB memory limit and 120 assertion
annotation bytes on both bindings, so differences in API defaults do not
change their configuration.

The records preserve span parentage, attributes and their encodings, numeric
and text log severity, and every raw metric measurement. Timestamps and
snapshot sizes are validated within each run before their values are masked.
`EXCEPTIONS.md` describes the exact normalization. The four telemetry rows are
PRESENT on all three backends (vm, dart2js and dart2wasm) in the baseline and
join the single M6 target owned by P-F2 in P-F3.

```sh
python3 -I tool/parity/test_telemetry.py
# With the runner's core dependency configured:
(cd packages/monty_parity && dart test test/telemetry_probes_test.dart \
  test/telemetry_recorder_test.dart test/telemetry_vm_test.dart)
```

To regenerate only these expectations with the supplied Python 3.12 oracle:

```sh
../oracle-venv/bin/python -I tool/parity/oracle/run_probes.py tool/parity/expected \
  telemetry_plain telemetry_print telemetry_external telemetry_manual \
  telemetry_error telemetry_dump_load
```

Then re-bind `manifest.json` using `tool/check_parity_manifest.py`'s `compute()`
and run the checker. On macOS keep `expected/b11fs_unlink_dir` from git; its
committed expectation is Linux's errno. The telemetry helper is included in
the manifest's generator hash.

R-P2 VM validation against core `4ea974136c80fae3d7e1a1bf2117585d9360d2e5`
(including the error-log integer timing fix):

- The full VM runner completes. All six telemetry scenarios MATCH, and all
  four telemetry rows are PRESENT. The score is 83/185 PRESENT (83/95
  re-measured), versus the baseline's 79. Every non-telemetry row and cell
  retains its baseline status.
- The three OTel requirements retain their exact committed versions and hashes
  in the parity-only lock. CI and the setup command above install both locks
  with hash verification; the manifest binds both. All four shared upstream
  files match core again.
- Oracle regeneration matches all 104 Python probes and 672/673 cases; only
  the documented macOS/Linux `b11fs_unlink_dir` errno differs. Expected
  outcomes and comparison rules are unchanged.
- All 13 telemetry Dart tests, analysis, formatting, DCM correctness checks,
  and offline telemetry checks pass. The original integer/string
  finding and its resolution are in
  [R-P2-core-findings.md](.gate-logs/R-P2-core-findings.md), with commands in
  [R-P2-validation.txt](.gate-logs/R-P2-validation.txt).

The host orchestrator owns baseline re-recording.
