// The `core` and `session` engines: one driver that runs a parity case
// through dart_monty_core's public `Monty` / `MontySession`
// (`Monty.checkout`, `MontySession.feedRun`). Same steps and the same
// normalised outcome as the `worker` engine (worker_runner.dart
// `runEngineCase`), for tool/parity/score.py. Web-safe.
//
// The two engines differ only in their rules, which the scorer applies: the
// `core` engine's outcomes are scored against tool/parity/baseline-current.json
// (whose rows may name an exception), the `session` engine's against the E0
// cases and the C1 gate with no named exception.
//
// The caller owns the pool: the VM runner (bin/parity_session_vm.dart) and
// the browser runner (test/parity_session_web_test.dart) each start one for
// the whole run, so a worker is Reset and reused from case to case, and a
// case that kills its worker is followed by a fresh one.
//
// The case settings are read into core's internal `SessionConfig` record and
// handed to the public `Monty.checkout` field by field.
// ignore_for_file: implementation_imports

import 'package:dart_monty_core/dart_monty_core.dart';
import 'package:dart_monty_core/src/worker/proto_session.dart';
import 'package:monty_parity/src/host_world.dart';
import 'package:monty_parity/src/worker_runner.dart';

/// The engines this driver runs, by the name their outcomes carry.
const sessionEngines = {'core', 'session'};

/// What the driver applies that the raw worker engine cannot: the case's
/// mounts, its snapshot/restore points and its assert-message setting.
const _applies = {'fs', 'restore_before', 'assert_message_annotations'};

/// Runs case [c] on [backend] through a session checked out of [pool], as
/// [engine] (`session` or `core`).
Future<Map<String, Object?>> runSessionCase(
  Map<String, Object?> c,
  String backend,
  Monty pool, {
  String engine = 'session',
}) {
  if (!sessionEngines.contains(engine)) {
    throw ArgumentError.value(engine, 'engine', 'not a session engine');
  }

  return runEngineCase(
    c,
    backend,
    engine: engine,
    engineVersion: 'dart_monty_core (public API: Monty / MontySession)',
    open: (config, host) async => _PoolCaseSession(
      pool,
      config,
      await _checkout(pool, config),
      host is SessionHost && host.active ? host : null,
    ),
    configure: sessionConfig,
    hostFor: SessionHost.new,
    applies: _applies,
  );
}

/// [config] through the public [Monty.checkout].
Future<MontySession> _checkout(Monty pool, SessionConfig config) =>
    pool.checkout(
      scriptName: config.scriptName,
      limits: config.limits,
      typeCheck: config.typeCheck,
      typeCheckStubs: config.typeCheckStubs,
      typeCheckFormat: config.typeCheckFormat,
      typeCheckColor: config.typeCheckColor,
      assertMessageAnnotations: config.assertMessageAnnotations,
      printFlushInterval: config.printFlushInterval,
      osPolicy: config.osPolicy,
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

  // Upstream's `assert_message_annotations=False` shows no operand bytes.
  final annotations = switch (c['assert_message_annotations']) {
    false => 0,
    final int n => n,
    _ => null,
  };
  final base = worker.limits;

  return SessionConfig(
    osPolicy: worker.osPolicy,
    assertMessageAnnotations: annotations,
    // No limit set sends no limits, as upstream's checkout without `limits`.
    limits: base == null && suspensions == null && sleep == null
        ? null
        : ResourceLimits(
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
  _PoolCaseSession(this._pool, this._config, this._session, this._host);

  final Monty _pool;
  final SessionConfig _config;
  MontySession _session;

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
    while (snapshot is! MontyComplete) {
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
        MontyComplete() => snapshot,
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
  Future<void> restore() async {
    final state = await _session.dump();
    await _session.close();
    _session = await _checkout(_pool, _config);
    await _session.loadSession(state);
  }

  @override
  Future<void> close() => _session.close();
}
