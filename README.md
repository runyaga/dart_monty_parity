# dart_monty_parity

Measures how far [dart_monty_core](https://github.com/runyaga/dart_monty_core) matches upstream
[pydantic/monty](https://github.com/pydantic/monty) 1.1.0, per capability row of
`tool/upstream/capability-manifest.tsv`, on the Dart VM (FFI), dart2js and dart2wasm.

The suite is self-contained: the cases, upstream's expected outcomes, the oracle that generates them,
the Dart runner, the scorer and the recorded baselines all live here. It runs against a dart_monty_core
checkout you name. dart_monty_core's CI checks this repository out at a pinned commit and asserts the
current baseline against every pull request.

## Compatible core

`CORE_REF` holds the dart_monty_core commit that `tool/parity/baseline-current.json` was recorded on;
CI's `current` job reproduces it there. A core commit that does not change parity behaviour passes too,
as long as its `tool/upstream` files equal the copies here (`tool/check_core_upstream.py`).

## Layout

| Path | What |
|---|---|
| `tool/parity/cases/<suite>/<id>.json` | parity cases: Python code or REPL feeds, typed inputs, scripted external / OS-call answers, limits, OS policy, mounts, host objects |
| `tool/parity/cases/display/<id>.json` | the display suite: errors whose expected outcome also records upstream's renderings (`display()` in each format, `str()`, the `exception()` type and arguments, the refusal of an unknown format). No engine runs them; dart_monty_core's traceback renderer tests read them (`tool/parity/oracle/test_display.py` checks they are recorded) |
| `tool/parity/expected/<id>/` | upstream's outcome for each case and probe: `ref-py.json` (pydantic-monty), `refmem-py.json` (upstream's in-memory filesystem), `ref-rust.json` (upstream monty's Rust API, three Rust-only probes) |
| `tool/parity/manifest.json` | binds every case (sha256) to the expected outcomes generated from it (sha256), plus the sha256 of the generator (the oracle's code) and of the upstream pins |
| `tool/parity/oracle/` | the reference oracle that generates `expected/`: `run_cases.py`, `run_probes.py`, `rust/` |
| `tool/parity/rows.json` | per capability row: its cases or probes, the status recorded at core 5873ec5 per backend, per-architecture expectations, and the named exceptions |
| `tool/parity/baseline-current.json` | the current baseline: every case's result within every re-measured row, every row's status, per backend, the PRESENT totals, per-architecture differences and the list of intentional moves (`deltas`) |
| `tool/parity/e0.json` | the E0 gate: the `e0` cases core's monty-proto worker must match upstream on, per backend, and the cases deferred from it |
| `tool/parity/c1-gate.json`, `test_gate.py` | the C1 gate: per capability row, the PR whose landing makes the row's `session` rule (in `rows.json`) gated through the session engine, and which of those PRs have landed; and its offline self-test |
| `tool/parity/m3-target.json`, `test_target.py` | the M3 parity target: the in-scope manifest rows the M3 milestone owns, each with the backends it must be PRESENT on, the rest `excluded` under a named clause, and a row's `allowed` entry where an approved exception lets it be non-PRESENT; `score.py --target` prints it (not fatal until `--assert-target`); and its offline self-test |
| `tool/parity/EXCEPTIONS.md` | every tolerated difference (named exception) and rule choice, see below |
| `tool/parity/run.py`, `score.py`, `probes_compare.py` | run the cases through core on a backend; score each row against `expected/` |
| `tool/parity/worker_compare.py`, `test_worker_compare.py` | the worker engine's comparison (shared objects and sandbox ids up to relabelling, strings exactly) and its offline self-test |
| `packages/monty_parity` | the Dart runner: the `core` engine (core's public API), the `worker` engine (core's internal monty-proto worker, `lib/src/worker_runner.dart`) and the `session` engine (core's internal pool session, `lib/src/session_runner.dart` and its probes, `lib/src/session_probes.dart`), VM and browser, plus the worker crash tests |
| `tool/upstream/` | copies of dart_monty_core's `tool/upstream`: the hash-pinned pydantic-monty lock, the upstream pin and the frozen capability manifest |
| `tool/check_parity_manifest.py` | offline check that `manifest.json` binds every case to its expected outcomes |
| `tool/check_core_upstream.py` | checks that a core checkout's `tool/upstream` equals the copies here |

## Running against a core checkout

Needs the Dart SDK 3.13.5 (the version every workflow pins; `dart format` output differs between
releases), Python 3.10+, Rust (core's build hook compiles its native library for the VM backend), and
Chrome for dart2js and dart2wasm.

```sh
git clone https://github.com/runyaga/dart_monty_core core
git -C core checkout "$(cat CORE_REF)"

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

The `session` engine runs the same E0 cases through core's internal pool session (`MontyPool.checkout`,
`PoolSession.feedRun`), the path that becomes core's public `Monty` / `MontySession` at the cutover. One pool
serves the whole run, so its worker is Reset and reused from case to case:

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
steps that cannot match, and why, are listed in `tool/parity/EXCEPTIONS.md`; `host_class_type_proxy` stays under
`cutover`. Identity `==` of core's wrappers is the named divergence `host-wrapper-identity-eq` there.

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
baseline was measured) and assert `--assert-rows --assert-baseline`. Expected under this suite's
rules: on arm64 FFI PRESENT 36/185, dart2js 35, dart2wasm 35; on x86_64 35/35/34 (see per-architecture
expectations in `EXCEPTIONS.md`). The manifest's frozen FFI number, 37, was scored more leniently;
`rows.json` `rescored` lists the rows that account for the difference. That core needs `npm ci` in its `js/` for the browser WASI worker, and on a toolchain
without its cross targets `RUSTUP_TOOLCHAIN=stable`.

## Checks (CI)

- **`manifest`** (`ci.yaml`, every change): `tool/check_parity_manifest.py` fails if a case changed
  without its expected outcome being regenerated (or the reverse), an expected outcome changed outside a
  regeneration, or the oracle's code or the upstream pins changed since the outcomes were generated;
  then `tool/parity/test_worker_compare.py`, the worker comparison's self-test, and
  `tool/parity/test_gate.py`, the C1 gate's.
- **`current`** (`ci.yaml`): core at `CORE_REF`, all three backends, `--assert-current`; also analyses
  the runner and checks the upstream copies against that core.
- **`baseline`** (`ci.yaml`): core `5873ec5`, all three backends, `--assert-rows --assert-baseline`.
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
/tmp/oracle/bin/pip install --require-hashes --only-binary :all: --no-deps -r tool/upstream/requirements.lock
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
baseline.

## Updating the baseline

The baseline moves only on purpose. When a core change is meant to change parity (a case or a row
moves):

1. Here: run the suite against the core change on all three backends and re-record with
   `score.py OUT --write-current tool/parity/baseline-current.json`. `rows`, `cells`, `present` and
   `totals` are recorded as measured on **arm64**; per-architecture differences live under
   `platforms` (`--write-current` keeps `platforms`, `subject` and `deltas`). Add the move to `deltas`
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
