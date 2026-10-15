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
import 'package:monty_parity/src/case_runner.dart';
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
}) async {
  if (!sessionEngines.contains(engine)) {
    throw ArgumentError.value(engine, 'engine', 'not a session engine');
  }
  final proxies = engine == 'core'
      ? await _sandboxProxies(pool, sandboxProxyNames(c))
      : const <String, MontyValue>{};

  return runEngineCase(
    c,
    backend,
    engine: engine,
    engineVersion: 'dart_monty_core (public API: Monty / MontySession)',
    open: (config, host) async => _PoolCaseSession(
      pool,
      config,
      host,
      await _checkout(pool, config),
    ),
    configure: sessionConfig,
    // Both build the case's host world through the public HostClass,
    // ClassInstance and ClassType (public since C5-s); the core engine makes
    // no value only an internal factory makes, so it takes the builtin types
    // and functions a case hands the sandbox from a feed that returns them.
    hostFor: engine == 'core'
        ? (c) => SessionHost(c, publicOnly: true)..sandboxProxies = proxies
        : SessionHost.new,
    applies: _applies,
  );
}

/// The builtin types and functions [names], as a feed on a session of its own
/// returns them: core's public API has no constructor for a type proxy, and
/// a host that needs `int` gets it from the sandbox. A name the feed cannot
/// give (not a builtin) is left out, and the case's value stays inexpressible.
Future<Map<String, MontyValue>> _sandboxProxies(
  Monty pool,
  Set<String> names,
) async {
  if (names.isEmpty) return const {};
  final sorted = names.toList()..sort();
  final session = await pool.checkout();
  try {
    final MontyTuple(:items) =
        await session.feedRun('(${sorted.join(', ')},)') as MontyTuple;

    return {for (var i = 0; i < sorted.length; i++) sorted[i]: items[i]};
  } on MontyError {
    return const {};
  } finally {
    await session.close();
  }
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
            maxTurnDuration: base?.maxTurnDuration,
            gcInterval: base?.gcInterval,
            maxSuspensions: suspensions,
            maxTotalSleep: sleep == null
                ? null
                : Duration(microseconds: (sleep * 1e6).round()),
          ),
  );
}

final class _PoolCaseSession implements CaseSession {
  _PoolCaseSession(this._pool, this._config, this._host, this._session);

  final Monty _pool;
  final SessionConfig _config;
  final CoreHost _host;
  MontySession _session;

  /// Runs [code] by hand, as the oracle drives upstream (feed_start, then
  /// one resume per suspension; tool/parity/oracle/run_cases.py): a method
  /// call or a lazy attribute of a host object is logged where it suspends,
  /// before the policy check, as the oracle's wrapper logs it; an OS call's
  /// answer is built before the feed resumes, so a value core refuses to
  /// build is the host's refusal ([HostAnswerRefused]), outside the sandbox,
  /// as upstream's host raises it. Everything else resumes as `feedRun`
  /// would (`resumeAuto`).
  @override
  Future<MontyValue> feed(
    String code, {
    required Map<String, MontyValue> inputs,
    required Map<String, ProtoExternalFunction> externals,
    required OsHandler onOsCall,
    required ProtoPrintCallback onPrint,
  }) async {
    // The OS answer built before the resume, replayed to core's resumeAuto.
    MontyValue Function()? built;
    var snap = await _session.feedStart(
      code,
      inputs: inputs,
      externalLookup: hostFunctions(externals),
      os: (name, args, kwargs) {
        final answer = built ?? (() => throw StateError('no OS answer built'));
        built = null;

        return answer();
      },
      printCallback: onPrint,
    );
    while (true) {
      switch (snap) {
        case MontyComplete(:final output):
          return output;
        case NameLookupSnapshot(:final String objectId, :final variableName):
          _host.logObjectLookup(objectId, variableName);
          snap = await snap.resumeAuto();
        case FunctionSnapshot(:final String objectId, :final functionName):
          _host.logObjectCall(objectId, functionName);
          snap = await snap.resumeAuto();
        case FunctionSnapshot(isOsFunction: true):
          try {
            final value = await onOsCall(
              snap.functionName,
              snap.args,
              snap.kwargs,
            );
            built = () => value;
          } on HostAnswerRefused {
            // Nothing is resumed: the suspended session is replaced.
            await _replace();
            rethrow;
          } on Object catch (e, st) {
            built = () => Error.throwWithStackTrace(e, st);
          }
          snap = await snap.resumeAuto();
        case final FunctionSnapshot f:
          snap = await f.resumeAuto();
        case final NameLookupSnapshot n:
          snap = await n.resumeAuto();
        case final FutureSnapshot f:
          snap = await f.resumeAuto();
      }
    }
  }

  /// A fresh session in place of one left suspended.
  Future<void> _replace() async {
    await _session.close();
    _session = await _checkout(_pool, _config);
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
