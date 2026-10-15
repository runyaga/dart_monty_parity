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
      // By hand only where the suspension point shows in the outcome: a
      // host world's call and lookup transcript, and a host refusal of an OS
      // answer. Under feedStart core hands system.sleep to the OS callback,
      // which feedRun services itself, so every other case runs as before.
      byHand:
          (host is SessionHost && host.active) || c['local_refusal'] == true,
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
  _PoolCaseSession(
    this._pool,
    this._config,
    this._host,
    this._session, {
    required this.byHand,
  });

  /// Whether [feed] resumes each suspension by hand; else it runs `feedRun`.
  final bool byHand;

  final Monty _pool;
  final SessionConfig _config;
  final CoreHost _host;
  MontySession _session;

  /// Runs [code]; with [byHand], by hand, as the oracle drives upstream
  /// (feed_start, then one resume per suspension;
  /// tool/parity/oracle/run_cases.py): a method
  /// call or a lazy attribute of a host object is logged where it suspends,
  /// before the policy check, as the oracle's wrapper logs it. Every
  /// suspension then resumes as `feedRun` would (`resumeAuto`, which consults
  /// [onOsCall] for an OS call core does not service itself). An OS answer
  /// core refuses to build ([HostAnswerRefused]) is the host's refusal, as
  /// upstream's host raises it before resuming: the feed's result is
  /// discarded, the session replaced, and the refusal thrown.
  @override
  Future<MontyValue> feed(
    String code, {
    required Map<String, MontyValue> inputs,
    required Map<String, ProtoExternalFunction> externals,
    required OsHandler onOsCall,
    required ProtoPrintCallback onPrint,
  }) async {
    if (!byHand) {
      return _session.feedRun(
        code,
        inputs: inputs,
        externalLookup: hostFunctions(externals),
        os: onOsCall,
        printCallback: onPrint,
      );
    }
    HostAnswerRefused? refused;
    Future<MontySnapshot> guarded(
      Future<MontySnapshot> Function() resume,
    ) async {
      try {
        final next = await resume();
        if (refused case final r?) throw r;

        return next;
      } on Object {
        if (refused case final r?) {
          await _replace();
          throw r;
        }
        rethrow;
      }
    }

    var snap = await guarded(
      () => _session.feedStart(
        code,
        inputs: inputs,
        externalLookup: hostFunctions(externals),
        // Synchronous, as the handler is: core reads a not-handled throw
        // from the call itself.
        os: (name, args, kwargs) {
          try {
            return onOsCall(name, args, kwargs);
          } on HostAnswerRefused catch (e) {
            refused = e;
            rethrow;
          }
        },
        printCallback: onPrint,
      ),
    );
    while (true) {
      switch (snap) {
        case MontyComplete(:final output):
          return output;
        case NameLookupSnapshot(:final String objectId, :final variableName):
          _host.logObjectLookup(objectId, variableName);
          snap = await guarded(snap.resumeAuto);
        case FunctionSnapshot(:final String objectId, :final functionName):
          _host.logObjectCall(objectId, functionName);
          snap = await guarded(snap.resumeAuto);
        case final FunctionSnapshot f:
          snap = await guarded(f.resumeAuto);
        case final NameLookupSnapshot n:
          snap = await guarded(n.resumeAuto);
        case final FutureSnapshot f:
          snap = await guarded(f.resumeAuto);
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
