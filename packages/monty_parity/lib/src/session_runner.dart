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
import 'package:dart_monty_core/src/worker/monty_snapshot.dart';
import 'package:dart_monty_core/src/worker/pool_session.dart';
import 'package:dart_monty_core/src/worker/proto_session.dart';
import 'package:dart_monty_core/src/worker/resource_limits.dart';
import 'package:monty_parity/src/host_world.dart';
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
  open: (config, host) async => _PoolCaseSession(
    await pool.checkout(options: config),
    host is SessionHost && host.active ? host : null,
  ),
  configure: sessionConfig,
  hostFor: SessionHost.new,
);

/// Case limits -> the session's [SessionConfig]: the worker engine's, plus
/// the suspension and sleep budgets the session enforces. A limit the
/// session has no field for is recorded under `unsupported`.
SessionConfig sessionConfig(
  Map<String, Object?> c,
  Map<String, Object?> unsupported,
) {
  const own = {'max_suspensions', 'max_total_sleep_secs'};
  final limits = ((c['limits'] as Map?) ?? const {}).cast<String, Object?>();
  final suspensions = limits['max_suspensions'] as int?;
  final sleep = limits['max_total_sleep_secs'] as num?;
  final worker = workerConfig({
    ...c,
    'limits': {
      for (final MapEntry(:key, :value) in limits.entries)
        if (!own.contains(key)) key: value,
    },
  }, unsupported);

  final base = worker.limits;
  if (suspensions == null && sleep == null) return worker;

  return SessionConfig(
    osPolicy: worker.osPolicy,
    limits: ResourceLimits(
      maxMemory: base?.maxMemory,
      maxRecursionDepth: base?.maxRecursionDepth,
      maxFeedDuration: base?.maxFeedDuration,
      maxSuspensions: suspensions,
      maxTotalSleep: sleep == null
          ? null
          : Duration(microseconds: (sleep * 1e6).round()),
    ),
  );
}

final class _PoolCaseSession implements CaseSession {
  _PoolCaseSession(this._session, this._host);

  final PoolSession _session;

  /// The case's host world, when it has one: the feed is then driven by
  /// hand, because a host function may answer with a host-object wrapper,
  /// which `feedRun`'s `ProtoExternalFunction` (a `MontyValue`) cannot carry.
  final SessionHost? _host;

  @override
  Future<MontyValue> feed(
    String code, {
    required Map<String, MontyValue> inputs,
    required Map<String, ProtoExternalFunction> externals,
    required OsHandler onOsCall,
    required ProtoPrintCallback onPrint,
  }) async {
    final host = _host;
    if (host == null) {
      return _session.feedRun(
        code,
        inputs: inputs,
        externals: externals,
        os: onOsCall,
        printCallback: onPrint,
      );
    }
    var snapshot = await _session.feedStart(
      code,
      inputs: inputs,
      externals: externals,
      os: onOsCall,
      printCallback: onPrint,
    );
    while (snapshot is! SessionComplete) {
      snapshot = switch (snapshot) {
        FunctionSnapshot(objectId: null, isOsFunction: false)
            when externals[snapshot.functionName] != null =>
          await _answer(
            snapshot,
            externals[snapshot.functionName]!,
            host,
          ),
        FunctionSnapshot() => await snapshot.resumeAuto(),
        NameLookupSnapshot() => await snapshot.resumeAuto(),
        FutureSnapshot() => await snapshot.resumeAuto(),
        SessionComplete() => snapshot,
      };
    }

    return snapshot.output;
  }

  /// Answers [call] with what [answer] gives: a value, or a host-object
  /// wrapper the host world built for it.
  Future<MontySnapshot> _answer(
    FunctionSnapshot call,
    ProtoExternalFunction answer,
    SessionHost host,
  ) async {
    final MontyValue value;
    try {
      value = await answer(call.args, call.kwargs);
      // Any throw but a chosen Python exception raises `Exception` with its
      // text, as `feedRun` raises it ([raisedFrom]).
    } on ExternalException catch (e) {
      return call.resume(e);
    } on Object catch (e) {
      return call.resume(ExternalException('Exception', '$e'));
    }

    return call.resume(ExternalReturnValue(host.takeWrapper() ?? value));
  }

  @override
  Future<void> close() => _session.close();
}
