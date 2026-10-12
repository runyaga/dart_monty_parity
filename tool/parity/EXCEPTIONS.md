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
