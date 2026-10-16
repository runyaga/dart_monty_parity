import 'dart:typed_data';

import 'package:dart_monty_core/dart_monty_core.dart';
import 'package:monty_parity/src/telemetry_recorder.dart';

/// R-P2's six scenarios, each with a fresh pool and reset recording buffers.
const telemetryProbeIds = {
  'telemetry_plain',
  'telemetry_print',
  'telemetry_external',
  'telemetry_manual',
  'telemetry_error',
  'telemetry_dump_load',
};

/// Runs under an ambient host span so callbacks and empty metric context
/// are checked independently of the span tree.
Future<void> runTelemetryProbe(
  String id,
  Future<Monty> Function() start,
  void Function(Map<String, Object?>) add,
) async {
  final recorder = parityTelemetry..reset();
  final sizes = <int>[];
  await recorder.runInContext('app-request', () async {
    final pool = await start();
    try {
      Uint8List? state;
      await pool.withSession(
        (session) async {
          if (id == 'telemetry_dump_load') {
            await session.feedRun('x = 42');
            final bytes = await session.dump();
            sizes.add(bytes.length);
            state = bytes;
          } else {
            await _scenario(id, session, add);
          }
        },
        assertMessageAnnotations: 120,
        limits: const ResourceLimits(maxMemory: 50000000),
      );
      final bytes = state;
      if (bytes != null) {
        await pool.withSession(
          (session) async {
            await session.loadSession(bytes);
            sizes.add(bytes.length);
            add({
              'ev': 'value',
              'value': (await session.feedRun('x') as MontyInt).value,
            });
          },
          assertMessageAnnotations: 120,
          limits: const ResourceLimits(maxMemory: 50000000),
        );
      }
    } finally {
      await pool.close();
    }
  });
  add(recorder.result(sizes));
}

Future<void> _scenario(
  String id,
  MontySession session,
  void Function(Map<String, Object?>) add,
) async {
  final recorder = parityTelemetry;
  switch (id) {
    case 'telemetry_plain':
      add({
        'ev': 'value',
        'value': (await session.feedRun('1 + 1') as MontyInt).value,
      });
    case 'telemetry_print':
      final printed = StringBuffer();
      await session.feedRun(
        "print('é🙂')",
        printCallback: (_, text) => printed.write(text),
      );
      add({'ev': 'printed', 'text': printed.toString()});
    case 'telemetry_external':
      final result = await session.feedRun(
        'ext(21)',
        externalLookup: {
          'ext': MontyHostFunction('ext', (args, kwargs) {
            add({
              'ev': 'callback_context',
              'name': recorder.contextName(recorder.currentContext()),
            });

            return MontyInt((args.single as MontyInt).value * 2);
          }),
        },
      );
      add({'ev': 'value', 'value': (result as MontyInt).value});
    case 'telemetry_manual':
      final snap = await session.feedStart('ext(21)') as FunctionSnapshot;
      add({
        'ev': 'trace_context',
        'name': recorder.contextName(snap.traceContext()),
      });
      final done =
          await snap.resume(const ExternalReturnValue(MontyInt(42)))
              as MontyComplete;
      add({'ev': 'value', 'value': (done.output as MontyInt).value});
      try {
        snap.traceContext();
        // A consumed snapshot must refuse context access.
        // ignore: avoid_catching_errors
      } on StateError {
        add({'ev': 'context_after_resume', 'refused': true});
      }
    case 'telemetry_error':
      try {
        await session.feedRun('1/0');
      } on MontyRuntimeError catch (error) {
        add({'ev': 'error_type', 'type': error.exception.excType});
      }
    default:
      throw ArgumentError.value(id, 'id', 'unknown telemetry scenario');
  }
}
