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

import 'dart:async';

import 'package:dart_monty_core/dart_monty_core.dart';
import 'package:dart_monty_core/src/worker/proto_session.dart';
import 'package:monty_parity/src/canon.dart';
import 'package:monty_parity/src/case_runner.dart';
import 'package:monty_parity/src/host_world.dart';
import 'package:monty_parity/src/mount_fixture.dart';
import 'package:monty_parity/src/worker_runner.dart';

/// The engines this driver runs, by the name their outcomes carry.
const sessionEngines = {'core', 'session', 'refmem'};

/// What the driver applies that the raw worker engine cannot: the case's
/// snapshot/restore points and its assert-message setting.
const _applies = {
  'restore_before',
  'assert_message_annotations',
  'fs.mounts',
  'close_mount_before_feed',
  'ctor_only',
  'rename_host_before_feed',
  'restore_snapshot_at',
};

/// Runs case [c] on [backend] through a session checked out of [pool], as
/// [engine] (`session`, `core` or the mount-free `refmem` column).
Future<Map<String, Object?>> runSessionCase(
  Map<String, Object?> c,
  String backend,
  Monty pool, {
  String engine = 'session',
}) async {
  if (!sessionEngines.contains(engine)) {
    throw ArgumentError.value(engine, 'engine', 'not a session engine');
  }
  final fixture = engine == 'refmem' ? MountFixture([]) : _fixture(c);
  if (fixture == null) {
    return newOutcome(c['id']! as String, engine, backend)
      ..['refused'] = {'where': 'MountDir', 'type': 'UnsupportedError'};
  }
  try {
    if (fixture.error != null || c['ctor_only'] == true) {
      final step = newStep();
      final error = fixture.error;
      if (error == null) {
        step['value'] = fixture.attributes;
      } else {
        step['exception'] = {
          'type': error.runtimeType.toString(),
          'message': '$error',
          'traceback': <Object?>[],
        };
        step['thrown'] = 'host';
      }
      return (fixture.stable(
                newOutcome(c['id']! as String, engine, backend)
                  ..['steps'] = [step],
              )!
              as Map)
          .cast<String, Object?>();
    }
    return (fixture.stable(await _run(c, backend, pool, engine, fixture))!
            as Map)
        .cast<String, Object?>();
  } finally {
    fixture.close();
  }
}

MountFixture? _fixture(Map<String, Object?> c) {
  try {
    return buildMountFixture(c);
    // Core's browser constructor is the refusal being measured.
    // ignore: avoid_catching_errors
  } on UnsupportedError {
    return null;
  }
}

Future<Map<String, Object?>> _run(
  Map<String, Object?> c,
  String backend,
  Monty pool,
  String engine,
  MountFixture fixture,
) async {
  final proxies = engine == 'core'
      ? await _sandboxProxies(pool, sandboxProxyNames(c))
      : const <String, MontyValue>{};

  return runEngineCase(
    engine == 'refmem' ? refmemCase(c) : c,
    backend,
    engine: engine,
    engineVersion: 'dart_monty_core (public API: Monty / MontySession)',
    open: (config, host) async => _PoolCaseSession(
      pool,
      config,
      host,
      await _checkout(pool, config),
      fixture,
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

/// The oracle's memory column seeds files without applying mount policy.
Map<String, Object?> refmemCase(Map<String, Object?> c) {
  final fs = (c['fs']! as Map).cast<String, Object?>();

  return {
    ...c,
    'fs': {
      for (final entry in fs.entries)
        if (entry.key != 'mounts') entry.key: entry.value,
    },
  };
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
    typeCheck: c['type_check'] == true,
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
    this._session,
    this._fixture, {
    required this.byHand,
  });

  /// Whether [feed] resumes each suspension by hand; else it runs `feedRun`.
  final bool byHand;

  final MountFixture _fixture;
  final Monty _pool;
  final SessionConfig _config;
  final CoreHost _host;
  MontySession _session;

  /// Runs [code]; with [byHand], by hand, as the oracle drives upstream
  /// (feed_start, then one resume per suspension;
  /// tool/parity/oracle/run_cases.py): a method
  /// call or a lazy attribute of a host object is logged where it suspends,
  /// before the policy check, as the oracle's wrapper logs it. An OS call's
  /// answer is built from [onOsCall] before the suspension resumes: one core
  /// refuses to build ([HostAnswerRefused]) is the host's refusal, as
  /// upstream's host raises it, so the suspension is abandoned unresumed, the
  /// session replaced and the refusal thrown. Every suspension then resumes
  /// as `feedRun` would (`resumeAuto`).
  @override
  Future<MontyValue> feed(
    String code, {
    required Map<String, MontyValue> inputs,
    required Map<String, ProtoExternalFunction> externals,
    required OsHandler onOsCall,
    required ProtoPrintCallback onPrint,
    String? cwd,
    Map<String, Object?> options = const {},
    List<Object?>? observations,
  }) async {
    final selected = (options['mount_indices'] as List?)?.cast<int>();
    final mounts = selected == null
        ? _fixture.mounts
        : [for (final i in selected) _fixture.mounts[i]];
    if (options['close_mount_before_feed'] == options['feed_index']) {
      for (final m in mounts) {
        m.close();
      }
    }
    final sleepAccess = options['sleep_access'] == true
        ? OSAccess(maxSleep: Duration.zero)
        : null;
    var handlerCalls = 0;
    FutureOr<MontyValue> handler(
      String name,
      List<MontyValue> args,
      Map<String, MontyValue> kwargs,
    ) {
      handlerCalls++;
      if (sleepAccess == null) return onOsCall(name, args, kwargs);
      observations?.add({'ev': 'handler', 'name': name});
      return sleepAccess(name, args, kwargs);
    }

    if (!byHand &&
        options['feed_start'] != true &&
        options['restore_snapshot_at'] == null) {
      return _session.feedRun(
        code,
        inputs: inputs,
        externalLookup: hostFunctions(externals),
        os: handler,
        mount: mounts,
        printCallback: onPrint,
        cwd: cwd,
        skipTypeCheck: options['skip_type_check'] == true,
      );
    }
    // An OS answer built before its suspension resumes, replayed to core.
    final answer = _OsAnswer(handler);
    var snap = await _session.feedStart(
      code,
      inputs: inputs,
      externalLookup: hostFunctions(externals),
      os: answer.call,
      mount: mounts,
      printCallback: onPrint,
      cwd: cwd,
      skipTypeCheck: options['skip_type_check'] == true,
    );
    if (options['record_dispatch'] == true && snap is FunctionSnapshot) {
      observations?.add({
        'ev': 'start',
        'name': snap.functionName,
        'is_os': snap.isOsFunction,
        'handler_calls': handlerCalls,
      });
    }
    final restoreAt = options['restore_snapshot_at'];
    if (restoreAt != null) {
      while (snap is FunctionSnapshot && snap.functionName != restoreAt) {
        snap = await snap.resumeAuto();
      }
      if (snap is! FunctionSnapshot || snap.functionName != restoreAt) {
        throw StateError('restore suspension $restoreAt not reached');
      }
      final state = await snap.dump();
      await _replace();
      snap = await _session.loadSnapshot(
        state,
        mount: options['restore_mount'] == false ? [] : mounts,
        externalLookup: hostFunctions(externals),
        os: answer.call,
        printCallback: onPrint,
      );
    }
    if (options['dispatch'] == 'manual' &&
        snap is FunctionSnapshot &&
        snap.isOsFunction) {
      snap = await snap.resume(
        ExternalReturnValue(
          await handler(snap.functionName, snap.args, snap.kwargs),
        ),
      );
    }
    return _resume(snap, answer, prepareOs: byHand && mounts.isEmpty);
  }

  Future<MontyValue> _resume(
    MontySnapshot initial,
    _OsAnswer answer, {
    required bool prepareOs,
  }) async {
    var snap = initial;
    while (true) {
      switch (snap) {
        case MontyComplete(:final output):
          return output;
        case NameLookupSnapshot(:final String objectId, :final variableName):
          _host.logObjectLookup(objectId, variableName);
          snap = await _host.resuming<MontySnapshot>(objectId, snap.resumeAuto);
        case FunctionSnapshot(:final String objectId, :final functionName):
          _host.logObjectCall(objectId, functionName);
          snap = await _host.resuming<MontySnapshot>(objectId, snap.resumeAuto);
        case final FunctionSnapshot f when f.isOsFunction:
          // The answer first: a value core refuses to build is the host's
          // refusal, and the suspension is abandoned unresumed.
          try {
            if (prepareOs) await answer.prepare(f);
          } on HostAnswerRefused {
            await _replace();
            rethrow;
          }
          snap = await f.resumeAuto();
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

/// One OS answer prepared before resuming, as the oracle's manual path does.
final class _OsAnswer {
  _OsAnswer(this._handler);

  final OsHandler _handler;
  MontyValue Function()? _built;

  FutureOr<MontyValue> call(
    String name,
    List<MontyValue> args,
    Map<String, MontyValue> kwargs,
  ) {
    final answer = _built;
    _built = null;

    return answer == null ? _handler(name, args, kwargs) : answer();
  }

  Future<void> prepare(FunctionSnapshot snapshot) async {
    try {
      final value = await _handler(
        snapshot.functionName,
        snapshot.args,
        snapshot.kwargs,
      );
      _built = () => value;
    } on HostAnswerRefused {
      rethrow;
    } on Object catch (error, stack) {
      _built = () => Error.throwWithStackTrace(error, stack);
    }
  }
}
