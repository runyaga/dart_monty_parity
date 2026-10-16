# Parity named exceptions and rule choices

Every difference the parity scorer tolerates is a named exception in `tool/parity/rows.json`, with the
stable ID used below. `score.py` accepts only these two (`KEPT_EXCEPTIONS`) and fails on a
`rows.json` that names any other. Nothing else is tolerated, and an exception applies to a case only
when the difference it names is actually present in that case. Evidence is in the
`parity-baseline-5873ec5` CI artifact (`<case>/core-<backend>.json`) against
`tool/parity/expected/<case>/`.

The historical 5873ec5 totals were measured with three exceptions. P-F1 retires
`rename-missing-message`: OSAccess must now match the refmem oracle's exact message,
and a rule that still names that exception fails validation. Historical baselines
are unchanged; their totals are not measurements of the F-1 runner.
`--assert-rows` or `--assert-baseline` explicitly selects the historical
`runner-0.23` instrument: B1 and `memory_filesystem` read `core-<backend>.json`
against `ref-py.json`. Its missing-source rename message matches that host
oracle exactly, so no retired message exception is restored. Current scoring
requires the separate `refmem` column even when a matching core file exists.

## Named exceptions

| ID | Rows (cases) | Fields | Observed difference | Rationale | Without it | Removed when |
|---|---|---|---|---|---|---|
| `payload-host-type` | `callback_files` (probe `api_callback_files`) | the write event's `type` field | Python `str` (pydantic-monty `CallbackFile`) vs Dart `String`; path, text, values and order are equal | the host language's type name for the same payload, not sandbox behaviour | ABSENT (recorded **PRESENT**) | not expected (inherently per binding) |
| `no-suspension-position` | `run_progress_variants`, `resume_os_call`, `resolve_pending_futures`, `feed_repl_suspendable` (their probes) | suspension `position` | core `null`; upstream filename + byte range | core exposes no suspension position, which `suspension_source_position` measures on its own (ABSENT); these rows compare kind, name, args, kwargs, resumed values and errors | ABSENT (recorded PARTIAL ×3, **PRESENT** for `resolve_pending_futures`) | core reports suspension positions |

## P-F1 F-1-window owner exception

Owner approval: P-F1, under FAST1(4)'s delegated F-1 work. This temporary
`m3-target.json` allowance accepts `os_environment` as PARTIAL on `vm`,
`dart2js` and `dart2wasm`, scoped solely to `x21_fallback_nonfs`. F-1 removes
the memory-backed MountDir; that case requests a host mount and is unsupported
until F-2 restores host-directory mounts. Every other cell of the row must
MATCH, including `b14_env`.

The row remains a target on all three backends. This is a target-status
allowance, not a comparison exception: no outcome is normalized or ignored.
The existing target gate rejects any nonmatching cell outside that scope and
rejects the allowance as STALE as soon as the row becomes PRESENT on a covered
backend. F-2 must remove the allowance for each restored backend, and remove
the entry when none remain. `test_target.py` registers this approval and checks
the exact scope and the failure on PRESENT.

## Rule choices

Not exceptions, but they decide statuses:

| Rows | Rule | Why |
|---|---|---|
| all | **strict PARTIAL**: a row is PARTIAL only when at least one case matches upstream in full (every compared field equal, after only the named exceptions above); a row with no such case is ABSENT, including one of REFUSED and diverging cases; REFUSED needs every case refused. Encoded in `score.py` (`row_status`, `KEPT_EXCEPTIONS`) | a field that merely resembles upstream's (the same text in another type, equal values without identity, a transcript that differs) is not parity |
| `host_class_type_proxy` | REFUSED only when core raises "a type cannot be sent back" (`h_b07`). `h_b08`, where core cannot build a class value at all ("MontyClassType is not a MontyValue"), is ABSENT, not REFUSED, so the row (REFUSED + ABSENT) is ABSENT | not being able to build the value is absence, not a refusal of a request |
| `resume_os_call` | PARTIAL: the not-handled probe records core's public substitute (`osCallNoHandlerDefault` + `resumeWithException`) as `unsupported` | core has no not-handled verb; reproducing upstream's error with a substitute is not parity of the API |
| `memory_filesystem` | the separate `refmem` engine outcome is compared to upstream's `OSAccess`+`MemoryFile` (`refmem-py.json`); compares value, exception type and message | the row is the in-memory filesystem; upstream's in-memory filesystem is the comparable oracle |
| B1 filesystem OS rows (all ten) | `refmem-<backend>.json` against `refmem-py.json` on dart2js and dart2wasm, and temporarily on VM until P-F2; compares values, exceptions, prints, OS/external calls and configuration (refmem has no host-call column); missing memory outcomes fail, with no fallback to the core column | web has no host mounts; F-1 also has no host mount on VM. P-F2 restores VM scoring against `MountDir` / `ref-py.json` |
| cases with `fs.mounts`; mount budget probes | `unsupported: fs.mounts` scores UNSUPPORTED and an all-unsupported row is ABSENT, until P-F2 | OSAccess has neither host mounts nor mount budgets. The separate refmem run seeds only files and applies no mount mode or cap |
| `interactive_continuation`, `compile_program`, `compiled_source` | oracle is upstream `monty`'s Rust API at the pinned commit (`ref-rust.json`) | no upstream binding exposes them |
| `file_mode_inspection`, `file_handle_value_conversion` | value and exception only, not the OS-call transcript | path-type differences in transcripts (str vs PurePosixPath) belong to `os_callback_adapter` |
| `raised_exception` / `traceback_frames` | exception type + message / frames only, over the 273 `b12_*` cases | each row is one part of an exception |
| `turn_execution_limit`, `host_sleep_budget`, `garbage_collection_interval`, `host_suspension_budget` | ABSENT, not REFUSED, when the limit has no `MontyLimits` field | a missing knob is absence, even where the web backend refuses session limits |
| `file_mode_inspection`, `file_handle_value_conversion` (cases marked `local_refusal`: `x8hostmode_zz`, `x8hostmode_rplus`, `x8hostmode_neg`) | a step whose upstream outcome is the host's own refusal to build an OS call's answer (`thrown: host`, or a `HOST:` type; no traceback, no value) matches a step the Dart host refused building that answer (`thrown: host`, no traceback, no value), whatever the type and message; any other step compares as usual (`score.py with_host_refusals`) | the host returned a file handle with a mode or position the binding refuses: upstream's host raises `ValueError`/`OverflowError` constructing it, core's `ArgumentError`; both refuse on the host before the feed resumes, as the worker engine's `local_refusal` rule matches host-side input refusals. The session driver resumes by hand, as the oracle does, so the refusal stays on the host |
| `os_entry_removal` | no exception | its one cross-platform difference (`b11fs_unlink_dir`) is upstream `MountDir` following the macOS errno; on the committed Linux expectations core matches |
| `boolean_integer_values` | no exception | core's own cases match |

## Rows rescored by these rules

`rows.json` `rescored` lists the rows whose status these rules decide differently from the capability
manifest's frozen baseline (and from the earlier scoring, which tolerated five more differences). Each
moved the same way at 5873ec5 and on current main, on both architectures:

| Row | Was | Now | Why |
|---|---|---|---|
| `lossy_output_placeholders` | PARTIAL | ABSENT | core returns repr/cycle/function placeholders as a typed opaque value, upstream as `str` with the same text; no case matches in full |
| `boundary_value_graph` | PARTIAL | ABSENT | core's decoded tree repeats a shared sub-object as equal copies, upstream marks it once; no case matches in full |
| `host_eager_attributes` | PRESENT | ABSENT | all three cases differ in the host-call transcript (upstream logs `convert_value` events and the method call's receiver); values, exceptions and prints are equal, but no case matches in full |
| `host_class_type_proxy` | REFUSED | ABSENT | `h_b08` is ABSENT, see the rule above |
| `os_calendar_calls`, `clock_source` | PARTIAL | ABSENT | `b14_date_callhost` records `os_policy` `call_host` as unsupported (core has no OsPolicy API); no case matches in full |
| `feed_execution_limit` | PARTIAL (FFI) | ABSENT (FFI) | `l06_feed_timeout` raises upstream's type with other wording (`time limit exceeded` vs `feed time limit exceeded`); `l08`/`l09` differ (core's feed budget does not reset per feed). Still REFUSED on the web, which declines the limit |

## Per-architecture expectations

Not an exception: recorded as measured. On x86_64, core returns `float("nan")` with the sign bit set
(`fff8000000000000`; upstream `7ff8000000000000`, observable in Python via `math.copysign`) on the VM and
dart2wasm, so `float_values` is PARTIAL there: FFI PRESENT 35/185, dart2js 35, dart2wasm 34. On arm64 (the
platform the manifest baseline was measured on) it is 36/35/35. `rows.json` and `baseline-current.json`
record both under `platforms`; any other deviation fails. Removed when core canonicalises NaN to upstream's
bits.

## Worker engine: comparison rules

The worker engine (`run.py --engine worker`; `score.py --assert-e0`, `--assert-worker`) is compared by
`tool/parity/worker_compare.py`, not by the core engine's `norm`, and tolerates no named exception:

| Rule | Why |
|---|---|
| Each root batch (a step's value; a call's arguments and keyword arguments together) is canonicalised with one identity memo, by the oracle (`canon_batch`) and the runner (`identityCanonBatch`) alike, and a feed's inputs are built with one memo on both sides. Containers, named tuples and class proxies carry identity, and the walk goes into proxy attributes | sharing across the arguments of one call, or across the inputs of one feed, is what upstream preserves (`shared_value_batches`) |
| Two values match when one bijection between the two sides' reference ids makes them equal, with set and frozenset members matched as multisets; a search revisits a pairing when something later contradicts it. The oracle numbers shared objects before it encodes: in document order, each set's members after everything outside sets and in their label-free order, so no number follows a set's iteration order; it refuses (a runner error) a set whose tied members would still be numbered in iteration order | each side numbers its references in its own walk order, and members that are structurally equal but distinct (tuples of distinct NaNs) cannot be told apart by sorting |
| Class and instance id fields (`instance_id`, `class_id`) match through one bijection per outcome; the oracle labels them `id#1`, `id#2`, ... in order of first appearance | equal ids stay equal and distinct ids distinct, across steps too, while the uuids themselves vary per run |
| Strings compare exactly: no uuid or number inside a string is rewritten. The oracle still rewrites them for the core engine's cases, and marks such an outcome `strings_rewritten`; the worker comparison scores it NO-ORACLE, so a case it compares is marked `exact_strings` | the core engine's rewrites hid a wrong id or a wrong count inside ordinary text |
| A time zone's name is upstream's: read through `__getinitargs__()`, so an explicit name equal to the generated `UTC+hh:mm`, or `""`, stays, and `timezone.utc` has none; inputs test the name by presence, not truthiness | the oracle used to drop both, and to turn `""` into no name |
| In a case marked `local_refusal`, a step whose upstream outcome is a host-side refusal (`HOST:<type>`: the host could not build the inputs, so nothing ran) matches a worker step refused locally with `ArgumentError`, whatever the two messages say | both hosts refuse before anything reaches the sandbox; the wording is each host language's |

`tool/parity/test_worker_compare.py` checks the comparison on every committed expected outcome (each
matches itself and a relabelled copy; every negative control that applies is caught) and on constructed
graphs. The core engine's path keeps its rules; it does not compare the id fields, which it never did.

## Worker engine: recorded divergences

`rows.json` `worker` records these; `score.py --assert-worker` prints them. A `diverge` case must differ
in exactly its recorded fields, with its recorded worker value, on every backend, so any other change
fails; it fails once it matches (it then moves to `pre_cutover`).

| ID | Rows (cases) | Observed difference | Reachable by a case | Removed when |
|---|---|---|---|---|
| `unicode-normalization` | `named_tuple_values` | CPython applies NFKC to a named tuple's field names when it builds the type; core has no Unicode normalization, so it accepts two field names that normalize alike (`K` and KELVIN SIGN), where CPython raises `SyntaxError`. The reference is the oracle's CPython 3.12, also for the rules that differ in 3.14 (names that normalize to `None`, `True` or `False`) | no: no sandbox emits such a named tuple, and upstream's host cannot build one as an input; core's own fixtures cover it | not planned (recorded by decision) |
| `unicode-version` | `exception_value_conversion` (`w_exc_unicode_version_in`, set `diverge`), `named_tuple_values`, `text_and_bytes` | core reads the platform's Unicode tables for Rust `{:?}` escaping in error messages, `str.isidentifier()` and `repr()` printability; measured against upstream over every code point, the only differences are characters one side's Unicode version assigns and the other's does not (CPython 3.12 has Unicode 15.0). A `KeyError` argument with an unpaired surrogate goes out as its `repr`, where core keeps U+1FAE9 and CPython escapes it | yes, through that `KeyError` on every backend; otherwise only through names and malformed arenas | the tables agree |

Cases of these rows that the worker engine does not run, each with its reason in `worker.not_run`:
`h_b05_std_type_proxy` (needs host classes and objects, which the worker driver has no registry for yet)
and the `x8handle_*`/`x8hostmode_neg` cases (their handle comes from `open()`, an OS call the worker
driver declines; `w_fh_in`, `w_fh_ret`, `w_fh_large_in` and `w_refuse_fh_position` carry handles through
inputs and callback returns instead).

The cutover set holds the complex values (`s07_complex` included) and file positions above 2^53
(`w_fh_large_in`; on the runner, a position an `int` cannot hold is recorded as `Inexpressible`). No
standalone class-type case is in it: upstream 1.1.0 sends a sandbox class reached as a value as its repr
text, which `w_classtype_out` checks before the cutover.

## Session engine: the C1 gate

The session engine (`run.py --engine session`; `score.py --assert-gate`, `tool/parity/c1-gate.json`)
tolerates no named exception. Its cases are compared by `worker_compare.py`, as the worker engine's are; its
probes by `probes_compare.py` with an empty rule, so `no-suspension-position` never applies: a suspension's
`position` (filename and UTF-8 byte range) is compared. A `session` rule in `rows.json` that names an
exception fails the gate.

Each row's `session` rule lists the core cases and probes of the row it runs, and under `not_run` the ones it
does not, each with its reason:

| Rows | Not run | Why |
|---|---|---|
| `host_sleep_budget` | `l14_total_sleep`, `l15_total_sleep_catch` | their expected messages have the amounts rewritten (`strings_rewritten`), which the comparison refuses; `ss_sleep_budget` measures the same with exact strings |
| `host_eager_attributes`, `host_lazy_attributes`, `host_constructor_policy`, `host_value_conversion_hook` | `h_x02_eager_list`, `h_x03_eager_none`, `h_x07_lazy`, `h_x11_class_methods`, `h_x14_convert_redact` | upstream's wrapper subclass logs a `lookup` or `call` before the policy check, also for a name or method the policy refuses, and a lazy read's `lookup` before its `convert`; core's wrappers are `final` and refuse before any host code runs, so the `host_calls` transcript lacks those entries. The `sp_host_*` probes measure the same refusals by outcome, and `sp_host_convert_lazy` the conversion of a lazy read |

Not reachable through the session engine (listed, not faked):

- An async host method (`async def`) driven from the sandbox: upstream's synchronous binding cannot drive one
  (the call never completes), so `sp_host_methods` and `sp_host_methods_all` leave it out. Core's own host oracle
  (`tool/generate_host_oracle.py`, on upstream's async binding) covers it.
- `h_b04_freed_proxy`: its expected message carries a sandbox instance id (`<uuid>` in the outcome) and the
  session comparison matches strings exactly. It is in no gated rule.

### Named divergence: `host-wrapper-identity-eq`

Core's `ClassInstance` and `ClassType` compare by identity and hash by identity (owner decision HOST1b).
Upstream's are dataclasses: wrappers with equal fields (one id included) are `==`, a wrapper with another id is
not, and neither is hashable (`hash` raises `TypeError`). The oracle probe `host_wrapper_eq` records those
answers (`expected/host_wrapper_eq`). No sandbox code reaches a wrapper's `==` or `hash`, so no case or probe of
the gate can differ on it; it is in no gate and core's own tests (`host_objects_test.dart`, group
`host-wrapper-identity-eq`) hold the Dart side. Removed when: never (a Dart object cannot be both equal by
fields and unhashable); the divergence stands.

## R-P2 telemetry normalization

These are measurement rules, not tolerated divergences. The six `telemetry_*`
probes feed `telemetry_adapter`, `telemetry_context`, `telemetry_metrics` and
`telemetry_instrumentation_lifecycle`. Installation is once per process/isolate;
scenario resets clear buffers, preserving the installed adapter. All existing
probes also run instrumented.

- Spans retain creation order and parent indices (the external host parent is
  `app-request`), names and attributes. `monty_version` must be `"1.1.0"` and
  `code.language` is compared. The default assertion annotation setting is
  supplied explicitly as 120 in both bindings.
- The timing attributes `total_execution_micros` and `feed_execution_micros`,
  `call_id` and `state_bytes` have masked values with their original types
  retained (`int`, `str`, etc.). Each dump/load `state_bytes` is checked against
  that run's actual dumped/loaded length before masking. `worker_pid` is
  omitted (Web Workers have no pid), together with `logfire.*`,
  `code.file.path`, `code.line.number`, `code.module.name`, `thread.*`,
  `busy_ns` and `idle_ns`.
- Span start/end and log timestamps must be epoch nanoseconds inside the run.
  Starts precede ends, child spans and logs lie inside their parents, and log
  timestamps are ordered. Their absolute values are then masked. Log body,
  attributes, parent, severity text and numeric severity remain compared.
- Metrics are sorted as a **multiset of raw measurements**, preserving every
  count and duplicate, kind, unit, description and attribute. Scheduling can
  interleave instruments; no SDK aggregation or deduplication is used. Seconds
  are masked. Print bytes and counter/up-down-counter increments are exact.
  Snapshot/frame byte values are masked because execution times change their
  varint lengths; counts and attributes are exact. Snapshot bytes must equal
  the run's own `dump()` length. Exact wire frame lengths remain core unit
  tests, since the public API cannot observe the frames. Every metric must
  explicitly use empty context even under the ambient host span.

The error-log timing type is **not** normalized away. Unlike tracing span
attributes, the `logfire::error!` SDK log path preserves this integer. Core
`4ea9741` corrects its error-log timing construction: all six telemetry probes
now MATCH and all four telemetry rows are PRESENT on vm. See
[the source-cited finding and resolution](../../.gate-logs/R-P2-core-findings.md).
OTel dependencies live in the parity-only oracle lock; the shared upstream
lock matches core.
