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
| `tool/parity/expected/<id>/` | upstream's outcome for each case and probe: `ref-py.json` (pydantic-monty), `refmem-py.json` (upstream's in-memory filesystem), `ref-rust.json` (upstream monty's Rust API, three Rust-only probes) |
| `tool/parity/manifest.json` | binds every case (sha256) to the expected outcomes generated from it (sha256), plus the sha256 of the generator (the oracle's code) and of the upstream pins |
| `tool/parity/oracle/` | the reference oracle that generates `expected/`: `run_cases.py`, `run_probes.py`, `rust/` |
| `tool/parity/rows.json` | per capability row: its cases or probes, the status recorded at core 5873ec5 per backend, per-architecture expectations, and the named exceptions |
| `tool/parity/baseline-current.json` | the current baseline: every case's result within every re-measured row, every row's status, per backend, the PRESENT totals, per-architecture differences and the list of intentional moves (`deltas`) |
| `tool/parity/EXCEPTIONS.md` | every tolerated difference (named exception) and rule choice, see below |
| `tool/parity/run.py`, `score.py`, `probes_compare.py` | run the cases through core on a backend; score each row against `expected/` |
| `packages/monty_parity` | the Dart runner (core's public API, VM and browser) |
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

To reproduce the measured baseline instead, check out core `5873ec5` (where the capability manifest's
baseline was measured) and assert `--assert-rows --assert-baseline`. Expected under this suite's
rules: on arm64 FFI PRESENT 36/185, dart2js 35, dart2wasm 35; on x86_64 35/35/34 (see per-architecture
expectations in `EXCEPTIONS.md`). The manifest's frozen FFI number, 37, was scored more leniently;
`rows.json` `rescored` lists the rows that account for the difference. That core needs `npm ci` in its `js/` for the browser WASI worker, and on a toolchain
without its cross targets `RUSTUP_TOOLCHAIN=stable`.

## Checks (CI)

- **`manifest`** (`ci.yaml`, every change): `tool/check_parity_manifest.py` fails if a case changed
  without its expected outcome being regenerated (or the reverse), an expected outcome changed outside a
  regeneration, or the oracle's code or the upstream pins changed since the outcomes were generated.
- **`current`** (`ci.yaml`): core at `CORE_REF`, all three backends, `--assert-current`; also analyses
  the runner and checks the upstream copies against that core.
- **`baseline`** (`ci.yaml`): core `5873ec5`, all three backends, `--assert-rows --assert-baseline`.
- **`Parity oracle`** (`parity-oracle.yaml`): regenerates every expected outcome from the hash-pinned
  oracle and fails on any byte of difference. Runs when a parity input changes (cases, expected outcomes,
  manifest, oracle code, upstream pins, the workflow), weekly, and on demand.

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
