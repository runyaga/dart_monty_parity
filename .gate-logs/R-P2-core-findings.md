# R-P2 VM findings, 2026-10-16

Measured core: `597659896f70616960b1c63d5b82ba7f6a438e3a` (F-1 merge).
Read-only upstream: `591527397445bf734873252d1ee20c1b6308fe7f`.
Parity starting point: `7739d193c94e9ff11bf1af18ffa50e7e4f04489c`.

The requested full VM run exited 0. The scorer reports 80/185 PRESENT
(80/95 re-measured), against baseline 79. All 91 non-telemetry measured
rows and all their cells exactly retain their baseline statuses.

| Telemetry probe | VM verdict | Classification |
|---|---|---|
| `telemetry_plain` | MATCH | No mismatch |
| `telemetry_print` | MATCH | No mismatch |
| `telemetry_external` | MATCH | No mismatch |
| `telemetry_manual` | MATCH | No mismatch |
| `telemetry_error` | DIVERGE | (b) Core error-log timing encoding |
| `telemetry_dump_load` | MATCH | No mismatch |

No (a) parity normalization or runner defect was found. No telemetry
expectation, type mask, comparison rule or runner was changed.

## (b) Error-log timing is incorrectly encoded as a String

The exhaustive recursive comparison of the six telemetry event arrays finds
exactly one difference:

```text
telemetry_error
events[1].logs[0].attributes.total_execution_micros.type
  Python: "int"
  core:   "str"
```

The Python oracle was regenerated with the pinned installed OTel packages;
all 104 Python probe files, including this expectation, are byte-identical.
The parity adapters retain the actual attribute type before masking the
timing value: [Python attributes()](../tool/parity/oracle/telemetry_probes.py)
lines 25–27 and [Dart _attributes/_type](../packages/monty_parity/lib/src/telemetry_recorder.dart)
lines 262–275. Neither coerces this field.

**Spec and upstream decision.** [Telemetry rule 2](spec/telemetry.md),
lines 117–128, requires upstream value encodings, including the distinction
between direct unsigned values in the `tracing` span path and signed values
passed to `span.record`. Its unsigned debug-string description must not be
applied indiscriminately to the separate log path:

- [tracing.rs:851–879](https://github.com/pydantic/monty/blob/591527397445bf734873252d1ee20c1b6308fe7f/crates/monty-pool/src/telemetry/tracing.rs#L851-L879)
  receives `micros: u64` but emits it through `logfire::error!`. This is not a
  `tracing` span creation or `span.record` operation.
- [telemetry/mod.rs:467–485](https://github.com/pydantic/monty/blob/591527397445bf734873252d1ee20c1b6308fe7f/crates/monty-pool/src/telemetry/mod.rs#L467-L485)
  forwards the SDK log record to `emit_log`.
- [monty-python telemetry.rs:580–586, 611–617](https://github.com/pydantic/monty/blob/591527397445bf734873252d1ee20c1b6308fe7f/crates/monty-python/src/telemetry.rs#L580-L617)
  maps log `AnyValue::Int` to a Python integer and `AnyValue::String` to a
  Python string. The regenerated oracle observes an integer. Together these
  establish that this log field reaches the adapter as an integer; converting
  the oracle to a string would hide a recorder difference. If rule 2's prose
  was intended to cover logs as well, that generalization conflicts with this
  measured upstream log path and needs clarification in the owning spec.
- The separate span rules remain correct:
  [tracing.rs:948–964](https://github.com/pydantic/monty/blob/591527397445bf734873252d1ee20c1b6308fe7f/crates/monty-pool/src/telemetry/tracing.rs#L948-L964)
  preserves native `i64` through `record_attr` and `int_attr`.
  [tracing.rs:922–942](https://github.com/pydantic/monty/blob/591527397445bf734873252d1ee20c1b6308fe7f/crates/monty-pool/src/telemetry/tracing.rs#L922-L942)
  preserves scalar arena integers; [tracing_json.rs:176–184](https://github.com/pydantic/monty/blob/591527397445bf734873252d1ee20c1b6308fe7f/crates/monty-pool/src/telemetry/tracing_json.rs#L176-L184)
  serializes integer nodes numerically. The matching `output`/`return_value`
  fields and unsigned call-span timing strings therefore need no coercion.

**Exact core change site:**
[`lib/src/worker/telemetry_attributes.dart:137`](https://github.com/runyaga/dart_monty_core/blob/597659896f70616960b1c63d5b82ba7f6a438e3a/lib/src/worker/telemetry_attributes.dart#L134-L146),
in `errorAttributes`, calls `eventTiming(event)` with its default
`signed: false`. That helper at lines 63–70 calls `toStringUnsigned()`.
`telemetry_events.dart:56–58` then emits those attributes as the error log.
Construct `total_execution_micros` as an integer for this error-log path
(the existing signed-count conversion handles the measured range), keeping
unsigned span encodings intact. Add a core regression test checking the
adapter receives an integer for the `1/0` error log. A blanket change of
`eventTiming`'s default would break the currently matching suspension spans.
Optional timing fields and other log kinds were not exercised by this probe;
their encoding should be checked independently before broadening the fix.

Core was not edited. The failing comparison and its negative control remain
active. `telemetry_adapter`, `telemetry_metrics`, and
`telemetry_instrumentation_lifecycle` are PARTIAL because their rules compare
this entire transcript; `telemetry_context` is PRESENT. This is not evidence
of a metric measurement failure: all raw metrics match, even in the error
scenario.

## Process-wide installation and baseline audit

The production VM entry point installs once before pool creation. Its full
probe process emitted 52 probes, including all six telemetry scenarios.
A separate process installed once and ran the six scenarios in normal then
reverse order: all 12 event arrays exactly match their full-suite recordings.
The known error-log mismatch is stable in both contexts.

A temporary copy of the VM entry point with only the installation/import
removed ran the other 46 probes. Every normalized outcome equals the
installed full-suite outcome, including errors and unsupported markers.
Under existing scoring rules, 40 MATCH. The baseline already has three
DIVERGE probes (`api_interactive_continuation`, `api_compile_program`,
`api_compiled_source`) and three UNSUPPORTED mount-budget probes
(`api_write_budget_feed1`, `api_write_budget_feed2`, `api_memory_budget`).
These cannot honestly be reported as MATCH; none changed with installation.
The existing callback-file host-type exception remains unchanged.

## Dependency integration

The orchestrator-supplied OTel lock hashes are committed with their computed
manifest binding. `tool/check_core_upstream.py --core ../rp2-core` still fails
because core's `tool/upstream/requirements.lock` lacks the identical OTel
addition. Mirror that file in core separately; this is a dependency-copy
integration issue, not an additional telemetry mismatch. No core files,
`CORE_REF`, baseline or M3 target were modified.

Web (`dart2js`, `dart2wasm`) was not run. See
[R-P2-validation.txt](R-P2-validation.txt) for commands and results.
