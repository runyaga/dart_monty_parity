# Parity named exceptions and rule choices

Every difference the parity scorer tolerates is a named exception in `tool/parity/rows.json`, with the
stable ID used below. `score.py` accepts only these three (`KEPT_EXCEPTIONS`) and fails on a
`rows.json` that names any other. Nothing else is tolerated, and an exception applies to a case only
when the difference it names is actually present in that case. Evidence is in the
`parity-baseline-5873ec5` CI artifact (`<case>/core-<backend>.json`) against
`tool/parity/expected/<case>/`.

**Why each one matters (measured):** with no exceptions, core `5873ec5` on arm64 scores FFI PRESENT
33/185 (dart2js 32, dart2wasm 32) instead of the recorded 36/35/35. Every exception changes at least one
row's status.

## Named exceptions

| ID | Rows (cases) | Fields | Observed difference | Rationale | Without it | Removed when |
|---|---|---|---|---|---|---|
| `rename-missing-message` | `memory_filesystem` (`b11fs_rename_missing`) | exception message | core `No such file or directory: '/data/nope' -> '/data/z.txt'`; upstream OSAccess `No such file or directory: '/data/nope'`. Same type | every other in-memory filesystem case is equal (58/59); core's wording matches upstream `MountDir` | PARTIAL (recorded **PRESENT**) | core uses OSAccess's wording |
| `payload-host-type` | `callback_files` (probe `api_callback_files`) | the write event's `type` field | Python `str` (pydantic-monty `CallbackFile`) vs core `VfsText`; path, text, values and order are equal | the host language's type name for the same payload, not sandbox behaviour | ABSENT (recorded **PRESENT**) | not expected (inherently per binding) |
| `no-suspension-position` | `run_progress_variants`, `resume_os_call`, `resolve_pending_futures`, `feed_repl_suspendable` (their probes) | suspension `position` | core `null`; upstream filename + byte range | core exposes no suspension position, which `suspension_source_position` measures on its own (ABSENT); these rows compare kind, name, args, kwargs, resumed values and errors | ABSENT (recorded PARTIAL ×3, **PRESENT** for `resolve_pending_futures`) | core reports suspension positions |

## Rule choices

Not exceptions, but they decide statuses:

| Rows | Rule | Why |
|---|---|---|
| all | **strict PARTIAL**: a row is PARTIAL only when at least one case matches upstream in full (every compared field equal, after only the named exceptions above); a row with no such case is ABSENT, including one of REFUSED and diverging cases; REFUSED needs every case refused. Encoded in `score.py` (`row_status`, `KEPT_EXCEPTIONS`) | a field that merely resembles upstream's (the same text in another type, equal values without identity, a transcript that differs) is not parity |
| `host_class_type_proxy` | REFUSED only when core raises "a type cannot be sent back" (`h_b07`). `h_b08`, where core cannot build a class value at all ("MontyClassType is not a MontyValue"), is ABSENT, not REFUSED, so the row (REFUSED + ABSENT) is ABSENT | not being able to build the value is absence, not a refusal of a request |
| `resume_os_call` | PARTIAL: the not-handled probe records core's public substitute (`osCallNoHandlerDefault` + `resumeWithException`) as `unsupported` | core has no not-handled verb; reproducing upstream's error with a substitute is not parity of the API |
| `memory_filesystem` | oracle is upstream's `OSAccess`+`MemoryFile` (`refmem-py.json`); compares value, exception type and message | the row is the in-memory filesystem; upstream's in-memory filesystem is the comparable oracle |
| `interactive_continuation`, `compile_program`, `compiled_source` | oracle is upstream `monty`'s Rust API at the pinned commit (`ref-rust.json`) | no upstream binding exposes them |
| `file_mode_inspection`, `file_handle_value_conversion` | value and exception only, not the OS-call transcript | path-type differences in transcripts (str vs PurePosixPath) belong to `os_callback_adapter` |
| `raised_exception` / `traceback_frames` | exception type + message / frames only, over the 273 `b12_*` cases | each row is one part of an exception |
| `turn_execution_limit`, `host_sleep_budget`, `garbage_collection_interval`, `host_suspension_budget` | ABSENT, not REFUSED, when the limit has no `MontyLimits` field | a missing knob is absence, even where the web backend refuses session limits |
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
| `host_class_type_proxy` | `h_b07_restore_type_proxy`, `h_b08_restore_class_eager` | they need a restore step the session engine does not run for cases and a class value coming out of the sandbox (`type(x)`), which core stages for the cutover; the row stays under `cutover` |

Not reachable through the session engine (listed, not faked):

- An async host method (`async def`) driven from the sandbox: upstream's synchronous binding cannot drive one
  (the call never completes), so `sp_host_methods` and `sp_host_methods_all` leave it out. Core's own host oracle
  (`tool/generate_host_oracle.py`, on upstream's async binding) covers it.
- A `ClassType` or a `ClassInstance` as a member of a container, or a host function that answers with one:
  the session takes a wrapper only as a feed input, a manual resume's value or what a wrapper's member returns,
  so the cases answer with a wrapper through a hand-driven feed (`host_world.dart`), not through `externals`.
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
