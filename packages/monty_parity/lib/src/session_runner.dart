// The `session` engine: drives one parity case through dart_monty_core's
// internal pool session (`MontyPool.checkout`, `PoolSession.feedRun`), the
// path that becomes core's public `Monty` / `MontySession` at the cutover.
// Same steps and the same normalised outcome as the `worker` engine
// (worker_runner.dart `runEngineCase`), for tool/parity/score.py. Web-safe.
//
// The caller owns the pool: the VM runner (bin/parity_session_vm.dart) and
// the browser runner (test/parity_session_web_test.dart) each start one for
// the whole run, so a worker is Reset and reused from case to case, and a
// case that kills its worker is followed by a fresh one.
//
// Implementation imports are the point of this engine: it measures core's
// internal session path before that path has a public API.
// ignore_for_file: implementation_imports
import 'package:dart_monty_core/dart_monty_core.dart';
import 'package:dart_monty_core/src/worker/monty_pool.dart';
import 'package:dart_monty_core/src/worker/pool_session.dart';
import 'package:dart_monty_core/src/worker/proto_session.dart';
import 'package:monty_parity/src/worker_runner.dart';

/// Runs case [c] on [backend] through a session checked out of [pool].
Future<Map<String, Object?>> runSessionCase(
  Map<String, Object?> c,
  String backend,
  MontyPool pool,
) => runEngineCase(
  c,
  backend,
  engine: 'session',
  engineVersion: 'dart_monty_core (internal: MontyPool session)',
  open: (config) async => _PoolCaseSession(
    await pool.checkout(options: config),
  ),
  configure: sessionConfig,
);

/// Case limits -> the session's [SessionConfig]: the worker engine's, plus
/// the suspension budget the session enforces. A limit the session has no
/// field for is recorded under `unsupported`.
SessionConfig sessionConfig(
  Map<String, Object?> c,
  Map<String, Object?> unsupported,
) {
  final limits = ((c['limits'] as Map?) ?? const {}).cast<String, Object?>();
  final suspensions = limits['max_suspensions'] as int?;
  final worker = workerConfig({
    ...c,
    'limits': {
      for (final MapEntry(:key, :value) in limits.entries)
        if (key != 'max_suspensions') key: value,
    },
  }, unsupported);

  return SessionConfig(
    maxMemoryBytes: worker.maxMemoryBytes,
    maxRecursionDepth: worker.maxRecursionDepth,
    maxFeedDuration: worker.maxFeedDuration,
    maxSuspensions: suspensions,
  );
}

final class _PoolCaseSession implements CaseSession {
  _PoolCaseSession(this._session);

  final PoolSession _session;

  @override
  Future<MontyValue> feed(
    String code, {
    required Map<String, MontyValue> inputs,
    required Map<String, ProtoExternalFunction> externals,
    required ProtoOsCallHandler onOsCall,
    required ProtoPrintCallback onPrint,
  }) => _session.feedRun(
    code,
    inputs: inputs,
    externals: externals,
    os: onOsCall,
    printCallback: onPrint,
  );

  @override
  Future<void> close() => _session.close();
}
