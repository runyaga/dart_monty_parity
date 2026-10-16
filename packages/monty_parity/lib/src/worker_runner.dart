// The `worker` engine: drives one parity case through dart_monty_core's
// monty-proto worker (its internal `ProtoSession` driver over a
// `MontyChannel`) and returns the same normalised outcome as the `core`
// engine, for tool/parity/score.py. Web-safe.
//
// The worker stack is internal to core (lib/src/worker, no public API yet),
// so this file imports it directly. The caller supplies the channel: a
// `ProcessChannel` on the VM (bin/parity_worker_vm.dart), a `WorkerChannel`
// in the browser (test/parity_worker_web_test.dart).
//
// [runEngineCase] is the engine-independent part: the `session` engine
// (session_runner.dart) runs the same steps through core's pool session.
//
// Implementation imports are the point of this engine: it measures core's
// internal worker path before that path has a public API.
// ignore_for_file: implementation_imports
import 'dart:async';
import 'dart:collection';
import 'dart:typed_data';

import 'package:dart_monty_core/dart_monty_core.dart'
    hide MontyError, MontySyntaxError, MontyTypingError;
import 'package:dart_monty_core/src/platform/monty_value.dart'
    show montyClassProxy, montyClassTypeProxy;
import 'package:dart_monty_core/src/worker/host_answers.dart' show HostAnswers;
import 'package:dart_monty_core/src/worker/monty_channel.dart';
import 'package:dart_monty_core/src/worker/monty_error.dart';
import 'package:dart_monty_core/src/worker/proto_session.dart';
import 'package:monty_parity/src/canon.dart';
import 'package:monty_parity/src/case_runner.dart';

/// Opens a fresh channel to a new worker.
typedef ChannelFactory = Future<MontyChannel> Function();

/// The case keys this engine has no way to apply. Each one present is
/// recorded under `unsupported` and the case runs without it.
const _inexpressible = {
  'host': 'no host objects in the worker driver; ran without them',
  'restore_before': 'no snapshot/restore in the worker driver',
  'close_mount_before_feed': 'no mounts in the worker driver',
  'ctor_only': 'no mounts in the worker driver',
  'rename_host_before_feed': 'no mounts in the worker driver',
  'restore_snapshot_at': 'no snapshots in the worker driver',
  'feed_start': 'requires the public session driver',
  'dispatch': 'requires the public session driver',
  'expect_os_snapshot': 'requires the public session driver',
  'record_dispatch': 'requires the public session driver',
  'sleep_access': 'requires the public session driver',
  'mount_indices': 'requires the public session driver',
  'restore_mount': 'requires the public session driver',
  'assert_message_annotations': 'no knob in the worker driver',
};

/// [functions] as core's `externalLookup` takes them: each a
/// [MontyHostFunction] named by its key.
Map<String, MontyValue> hostFunctions(
  Map<String, ProtoExternalFunction> functions,
) => {
  for (final MapEntry(:key, :value) in functions.entries)
    key: MontyHostFunction(key, value),
};

/// One case's session, whichever engine opened it.
abstract interface class CaseSession {
  /// Runs [code] and completes with its value.
  Future<MontyValue> feed(
    String code, {
    required Map<String, MontyValue> inputs,
    required Map<String, ProtoExternalFunction> externals,
    required OsHandler onOsCall,
    required ProtoPrintCallback onPrint,
    String? cwd,
    Map<String, Object?> options = const {},
    List<Object?>? observations,
  });

  /// Moves the session's state to a fresh session (dump, then load into a
  /// new checkout), as the case's `restore_before` asks. Only an engine that
  /// lists `restore_before` in its `applies` is asked.
  Future<void> restore();

  /// Ends the session.
  Future<void> close();
}

/// An intentional closed mount refused at the initial feed boundary.
final class ClosedMountRefused implements Exception {
  /// Retains the host error without catching unrelated runner StateErrors.
  const ClosedMountRefused(this.error);

  /// Core's closed-root feed refusal.
  final StateError error;
}

/// The host could not build an OS call's answer: [error] is core's refusal of
/// the value. A driver that resumes by hand (session_runner.dart) stops the
/// feed here, as the oracle's host does; a driver that answers from inside the
/// callback lets core raise it in the sandbox, with [error]'s text.
final class HostAnswerRefused implements Exception {
  /// Core refused the answer with [error].
  const HostAnswerRefused(this.error);

  /// Core's refusal.
  final ArgumentError error;

  @override
  String toString() => '$error';
}

/// Opens a session configured as [config] says.
typedef OpenCaseSession =
    Future<CaseSession> Function(
      SessionConfig config,
      CoreHost host,
    );

/// A case's settings as an engine's session takes them; a setting the engine
/// cannot apply is recorded in the second argument, `unsupported`.
typedef CaseConfig =
    SessionConfig Function(
      Map<String, Object?> c,
      Map<String, Object?> unsupported,
    );

/// Runs case [c] on [backend] through a worker [spawn] opens.
Future<Map<String, Object?>> runWorkerCase(
  Map<String, Object?> c,
  String backend,
  ChannelFactory spawn,
) => runEngineCase(
  c,
  backend,
  engine: 'worker',
  engineVersion: 'dart_monty_core (internal: ProtoSession)',
  open: (config, host) async {
    final channel = await spawn();
    try {
      return _ProtoCaseSession(await ProtoSession.open(channel, config));
      // A setting refused before Configure is sent (an unknown timezone name,
      // checked against the host's zone database) leaves the channel to its
      // owner, which is this runner: it discards the worker, as upstream's
      // failed checkout does, and the refusal is the case's outcome.
    } on Object {
      channel.kill();
      rethrow;
    }
  },
);

/// Runs case [c] on [backend] as [engine], through a session [open] opens
/// with the settings [configure] reads from the case, and returns its
/// normalised outcome.
Future<Map<String, Object?>> runEngineCase(
  Map<String, Object?> c,
  String backend, {
  required String engine,
  required String engineVersion,
  required OpenCaseSession open,
  CaseConfig configure = workerConfig,
  CoreHost Function(Map<String, Object?> c) hostFor = CoreHost.new,
  Set<String> applies = const {},
}) async {
  final out = newOutcome(c['id']! as String, engine, backend);
  out['engine_version'] = engineVersion;
  final unsupported = out['unsupported']! as Map<String, Object?>;
  final host = hostFor(c);
  for (final MapEntry(:key, :value) in _inexpressible.entries) {
    // An empty block (`host: {}`) asks for nothing, and a host world applies
    // its `host` block.
    if (key == 'host' && host.appliesHost) continue;
    if (applies.contains(key)) continue;
    final requested =
        [
          c,
          ...((c['feeds'] as List?) ?? []).whereType<Map<Object?, Object?>>(),
        ].any(
          (options) =>
              options[key] != null &&
              !(options[key] is Map && (options[key]! as Map).isEmpty),
        );
    if (!requested) continue;
    unsupported[key] = value;
  }
  final config = configure(c, unsupported);
  // One memo: inputs may share objects with each other, as the oracle's do.
  final memo = <int, MontyValue>{};
  var inputs = const <String, MontyValue>{};
  Inexpressible? inexpressible;
  ArgumentError? refused;
  try {
    inputs = {
      for (final MapEntry(:key, :value)
          in ((c['inputs'] as Map?) ?? const {})
              .cast<String, Object?>()
              .entries)
        key: _input((value! as Map).cast(), host, memo),
    };
  } on Inexpressible catch (e) {
    // An input this runner cannot build: the first feed records it, so the
    // case still has an outcome.
    inexpressible = e;
    // A value core's constructor refuses (a file position outside
    // 0..2^64-1, a mode upstream refuses): refused locally, as the driver
    // refuses a value it cannot send; the first feed records it.
    // ignore: avoid_catching_errors
  } on ArgumentError catch (e) {
    refused = e;
  }
  final script = Script(c);
  final steps = out['steps']! as List<Object?>;
  final restoreBefore = applies.contains('restore_before')
      ? ((c['restore_before'] as List?) ?? const []).cast<int>()
      : const <int>[];
  // The case's mounts, built once: their state lasts across its feeds. A
  // call no mount serves falls through to the scripted answers.
  var curOs = <Object?>[];
  MontyValue scriptedOs(
    String name,
    List<MontyValue> args,
    Map<String, MontyValue> kwargs,
  ) {
    curOs.add(_callRecord(name, args, kwargs));
    final a = script.next(name, isOs: true);
    if (a == null) throw const OsCallNotHandledException();
    try {
      return _answer(a, name, host);
      // A value core's constructor refuses (a file mode upstream refuses, a
      // position outside 0..2^64-1): the host could not build its answer.
      // ignore: avoid_catching_errors
    } on ArgumentError catch (e) {
      throw HostAnswerRefused(e);
    }
  }

  OsHandler? fsHandler;
  Object? fsError;
  final fs = (c['fs'] as Map?)?.cast<String, Object?>();
  if (fs != null &&
      !(applies.contains('fs.mounts') && fs.containsKey('mounts'))) {
    try {
      fsHandler = buildFsHandler(
        fs,
        scriptedOs,
        unsupported,
        scriptedEnvironment: engine != 'refmem',
      );
    } on Object catch (e) {
      fsError = e;
    }
  }
  final CaseSession session;
  try {
    session = await open(config, host);
    // The session's settings are refused before any worker runs a feed (an
    // unknown timezone name): the refusal is the case's one step, as the
    // oracle records a refused checkout.
    // ignore: avoid_catching_errors
  } on ArgumentError catch (e) {
    final step = newStep();
    step['exception'] = {
      'type': 'ArgumentError',
      'message': '${e.message}',
      'traceback': <Object?>[],
    };
    step['thrown'] = 'ArgumentError';
    steps.add(step);

    return out;
  }
  try {
    final feeds = (c['feeds'] as List?) ?? [c['code']! as String];
    for (var n = 0; n < feeds.length; n++) {
      final feed = feeds[n];
      final options = <String, Object?>{
        ...c,
        if (feed is Map) ...feed.cast<String, Object?>(),
        'feed_index': n,
      };
      final step = newStep();
      steps.add(step);
      host.log = step['host_calls']! as List<Object?>;
      final prints = <(String, String)>[];
      final ext = step['ext_calls']! as List<Object?>;
      curOs = step['os_calls']! as List<Object?>;
      if (restoreBefore.contains(n)) await session.restore();
      if (fsError != null) {
        step['exception'] = {
          'type': fsError.runtimeType.toString(),
          'message': '$fsError',
          'traceback': <Object?>[],
        };
        step['thrown'] = 'host';
        host.rawOutputs.add(null);
        continue;
      }
      if (n == 0 && refused != null) {
        step['exception'] = {
          'type': 'ArgumentError',
          'message': '${refused.message}',
          'traceback': <Object?>[],
        };
        step['thrown'] = 'ArgumentError';
        host.rawOutputs.add(null);
        continue;
      }
      if (n == 0 && inexpressible != null) {
        step['exception'] = {
          'type': 'Inexpressible',
          'message': inexpressible.message,
          'traceback': <Object?>[],
        };
        step['thrown'] = 'Inexpressible';
        host.rawOutputs.add(null);
        continue;
      }
      try {
        final value = await session.feed(
          feed is String ? feed : options['code']! as String,
          cwd: options['cwd'] as String?,
          options: options,
          observations: step['host_calls']! as List<Object?>,
          inputs: n == 0 ? inputs : const {},
          externals: {
            for (final name in script.externals.keys)
              name: (args, kwargs) {
                ext.add(_callRecord(name, args, kwargs));

                return _answer(script.next(name, isOs: false), name, host);
              },
          },
          onOsCall: fsHandler ?? scriptedOs,
          onPrint: (stream, text) => prints.add((stream, text)),
        );
        step['value'] = identityCanon(value, hostLabel: host.labelOf);
        host.rawOutputs.add(value);
      } on HostAnswerRefused catch (e) {
        // Refused on the host while building an OS call's answer, before
        // anything was resumed: the oracle's host raises there too
        // (`thrown: host`, no traceback).
        step['exception'] = {
          'type': 'ArgumentError',
          'message': '${e.error.message}',
          'traceback': <Object?>[],
        };
        step['thrown'] = 'host';
        host.rawOutputs.add(null);
      } on MontyRuntimeError catch (e) {
        step['exception'] = raisedDisplayed(
          e.display,
          e.traceback,
          e.exception.attributes,
        );
        step['thrown'] = 'MontyRuntimeError';
        host.rawOutputs.add(null);
      } on MontyTypingError catch (e) {
        step['exception'] = {
          'type': 'MontyTypingError',
          'message': e.display(),
          'traceback': <Object?>[],
        };
        step['thrown'] = 'MontyTypingError';
        host.rawOutputs.add(null);
        // A closed mount is a host-state refusal.
      } on ClosedMountRefused catch (e) {
        step['exception'] = {
          'type': 'StateError',
          'message': e.error.message,
          'traceback': <Object?>[],
        };
        step['thrown'] = 'host';
        host.rawOutputs.add(null);
      } on MontySyntaxError catch (e) {
        step['exception'] = raisedDisplayed(
          e.display,
          e.traceback,
          e.exception.attributes,
        );
        step['thrown'] = 'MontySyntaxError';
        host.rawOutputs.add(null);
        // A dead worker is an outcome here.
      } on MontyCrashedError catch (e) {
        // The worker is gone: no later feed can run.
        step['exception'] = {
          'type': 'MontyCrashedError',
          'message': '$e',
          'traceback': <Object?>[],
        };
        step['thrown'] = 'MontyCrashedError(${e.cause.name})';
        step['prints'] = mergePrints(prints);
        break;
        // A value the driver refuses to send (an unhashable key, a temporal
        // out of range, an unknown name, a non-canonical uuid): refused
        // locally, before anything reaches the worker; the session carries
        // on. Only the inputs are written outside a guarded callback.
        // ignore: avoid_catching_errors
      } on ArgumentError catch (e) {
        step['exception'] = {
          'type': 'ArgumentError',
          'message': '${e.message}',
          'traceback': <Object?>[],
        };
        step['thrown'] = 'ArgumentError';
        host.rawOutputs.add(null);
        // A value outside the driver's subset: the driver's own refusal is
        // the outcome, and the session carries on.
        // ignore: avoid_catching_errors
      } on UnsupportedError catch (e) {
        step['exception'] = {
          'type': 'UnsupportedError',
          'message': '${e.message}',
          'traceback': <Object?>[],
        };
        step['thrown'] = 'UnsupportedError';
        host.rawOutputs.add(null);
      }
      step['prints'] = mergePrints(prints);
    }
  } finally {
    await session.close();
  }

  return out;
}

final class _ProtoCaseSession implements CaseSession {
  _ProtoCaseSession(this._session);

  final ProtoSession _session;

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
    final result = await _session.drive(
      _session.prepareFeed(
        code,
        inputs,
        cwd: cwd,
        skipTypeCheck: options['skip_type_check'] == true,
      ),
      HostAnswers(
        externalLookup: hostFunctions(externals),
        os: onOsCall,
        objects: _session.objects,
      ),
      onPrint: onPrint,
    );

    return result.value;
  }

  @override
  Future<void> restore() =>
      throw UnsupportedError('no snapshot/restore in the worker driver');

  @override
  Future<void> close() => _session.close();
}

/// Case limits -> the worker's [SessionConfig]. A limit the worker has no
/// field for is recorded under `unsupported`.
SessionConfig workerConfig(
  Map<String, Object?> c,
  Map<String, Object?> unsupported,
) {
  int? memory;
  int? recursion;
  int? gcInterval;
  Duration? feed;
  Duration? turn;
  Duration seconds(Object? value) =>
      Duration(microseconds: ((value! as num) * 1e6).round());
  final limits = ((c['limits'] as Map?) ?? const {}).cast<String, Object?>();
  for (final MapEntry(:key, :value) in limits.entries) {
    switch (key) {
      case 'max_memory':
        memory = value! as int;
      case 'max_recursion_depth':
        recursion = value! as int;
      case 'gc_interval':
        gcInterval = value! as int;
      case 'max_duration_secs' || 'max_feed_duration_secs':
        feed = seconds(value);
      case 'max_turn_duration_secs':
        turn = seconds(value);
      default:
        unsupported['limits.$key'] = 'the worker driver has no $key';
    }
  }

  final osPolicy = _osPolicy(c, unsupported);

  // No limit set sends no limits, as upstream's checkout without `limits`.
  if (memory == null &&
      recursion == null &&
      gcInterval == null &&
      feed == null &&
      turn == null) {
    return SessionConfig(osPolicy: osPolicy);
  }

  return SessionConfig(
    limits: ResourceLimits(
      maxMemory: memory,
      maxRecursionDepth: recursion,
      gcInterval: gcInterval,
      maxFeedDuration: feed,
      maxTurnDuration: turn,
    ),
    osPolicy: osPolicy,
  );
}

/// A case's `os_policy` as core's [OsPolicy], or null when it has none. A
/// fixed datetime is read only in its naive form (`YYYY-MM-DDTHH:MM[:SS[.f]]`,
/// no offset), as a UTC instant, as upstream reads a naive one. A form the
/// case file spells but this reader does not build (a fixed datetime with an
/// offset, or other than naive, or whose microseconds the platform's
/// `DateTime` cannot hold) is recorded under `unsupported`, and the policy
/// is left out.
OsPolicy? _osPolicy(Map<String, Object?> c, Map<String, Object?> unsupported) {
  final p = (c['os_policy'] as Map?)?.cast<String, Object?>();
  if (p == null) return null;
  String? mode(String key) => p[key] is String ? p[key]! as String : null;
  RandomSeed? seed;
  if (p['random_start'] case final Map<Object?, Object?> start) {
    final s = (start['seed']! as Map).cast<String, Object?>();
    seed = switch (s['t']) {
      'int' => RandomSeed.integer(BigInt.parse(s['v']! as String)),
      'str' => RandomSeed.string(s['v']! as String),
      'bytes' => RandomSeed.bytes(_hex(s['hex']! as String)),
      'float' => RandomSeed.float(double.parse(s['v']! as String)),
      _ => null,
    };
    if (seed == null) {
      unsupported['os_policy'] = 'a seed of kind ${s['t']}';

      return null;
    }
  }
  DateTime? fixed;
  if (p['datetime'] case {'fixed': final String iso}) {
    fixed = _fixedInstant(iso);
    if (fixed == null) {
      unsupported['os_policy'] =
          'a fixed datetime other than a naive one this DateTime holds exactly';

      return null;
    }
  }

  return OsPolicy(
    datetime: mode('datetime'),
    fixedDatetime: fixed,
    timezone: mode('timezone'),
    sleep: mode('sleep'),
    processTime: mode('process_time'),
    randomStart: mode('random_start'),
    randomSeed: seed,
  );
}

/// [iso], a naive ISO 8601 datetime (`YYYY-MM-DDTHH:MM[:SS[.f]]`, no
/// offset), as a UTC [DateTime]; null for any other form, or when this
/// platform's [DateTime] cannot hold its microseconds.
DateTime? _fixedInstant(String iso) {
  final naive = RegExp(
    r'^\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d(?:\.(\d+))?)?$',
  ).firstMatch(iso);
  if (naive == null) return null;
  final instant = DateTime.parse('${iso}Z');
  final fraction = naive.group(1) ?? '';
  final micros = int.parse(fraction.padRight(6, '0').substring(0, 6));

  return instant.millisecond * 1000 + instant.microsecond == micros
      ? instant
      : null;
}

Uint8List _hex(String hex) => Uint8List.fromList([
  for (var i = 0; i < hex.length; i += 2)
    int.parse(hex.substring(i, i + 2), radix: 16),
]);

/// A call's record: its arguments and keyword arguments canonicalised as
/// one batch, as the oracle's `canon_call` does.
Map<String, Object?> _callRecord(
  String name,
  List<MontyValue> args,
  Map<String, MontyValue> kwargs,
) {
  final keys = kwargs.keys.toList();
  final c = identityCanonBatch([...args, for (final k in keys) kwargs[k]!]);

  return {
    'name': name,
    'args': c.sublist(0, args.length),
    'kwargs': {
      for (var i = 0; i < keys.length; i++) keys[i]: c[args.length + i],
    },
  };
}

/// The canonical kinds the worker engine builds itself: a file handle, and
/// a sandbox class instance (no public constructor) the host names by id
/// (`instance_id`, `class_id`), which the worker resolves, or the driver
/// refuses when the id is not a canonical uuid.
CanonExtra _workerExtra(CoreHost host) =>
    (c) => switch (c['t']) {
      'filehandle' => MontyFileHandle(
        c['path']! as String,
        c['mode']! as String,
        position: BigInt.parse(c['position']! as String),
      ),
      'proxy' =>
        host.internal('a sandbox class proxy') ??
            montyClassProxy(
              classType: montyClassTypeProxy(
                name: c['class']! as String,
                id: c['class_id']! as String,
                hostDefined: false,
                isDataclass: (c['dataclass'] as bool?) ?? false,
                attrs: const {},
              ),
              instanceId: c['instance_id']! as String,
              attrs: const {},
            ),
      _ => null,
    };

/// Canonical input [c] -> [MontyValue], sharing [memo] across the inputs.
MontyValue _input(
  Map<String, Object?> c,
  CoreHost host,
  Map<int, MontyValue> memo,
) => canonToMonty(c, host, memo, _workerExtra(host));

/// A scripted answer [a] for the call to [name], as the driver takes it.
MontyValue _answer(Map<String, Object?>? a, String name, CoreHost host) {
  if (a == null) {
    throw ExternalException('NameError', "name '$name' is not defined");
  }
  if (a['return'] case final Map<Object?, Object?> ret) {
    return canonToMonty(ret.cast(), host, null, _workerExtra(host));
  }
  if (a['echo'] case final int step) {
    return host.rawOutputs[step] ??
        (throw StateError('step $step produced no value to echo'));
  }
  final r = ((a['raise'] as Map?) ?? const {}).cast<String, Object?>();
  if (r.isEmpty) throw UnsupportedError('scripted answer $a');
  // An untyped throw: the host throws a value that names no Python type, as
  // a Dart function does; the oracle column raises `type` with `message`.
  // ignore: only_throw_errors, a string is the untyped value a host may throw
  if (r['untyped'] == true) throw r['message'] ?? '';
  throw ExternalException(
    (r['type'] as String?) ?? 'Exception',
    (r['message'] as String?) ?? '',
  );
}

/// Whether [v] carries identity: a container, a named tuple or a class
/// instance, each of which the oracle's `canon_batch` marks once when shared.
bool _isShared(MontyValue v) =>
    v is MontyList ||
    v is MontyTuple ||
    v is MontyDict ||
    v is MontySet ||
    v is MontyFrozenSet ||
    v is MontyNamedTuple ||
    v is MontyClassProxy ||
    v is MontyClassTypeProxy;

List<MontyValue> _children(MontyValue v) => switch (v) {
  MontyList(:final items) || MontyTuple(:final items) => items,
  MontySet(:final items) || MontyFrozenSet(:final items) => items,
  MontyDict(:final pairs) => [
    for (final (k, x) in pairs) ...[k, x],
  ],
  MontyNamedTuple(:final values) => values,
  MontyClassProxy(:final attrs) ||
  MontyClassTypeProxy(:final attrs) => [...attrs.values],
  _ => const [],
};

/// [MontyValue] -> canonical form, identity-aware as the oracle's `canon` is.
Object? identityCanon(
  MontyValue root, {
  String? Function(MontyValue)? hostLabel,
}) => identityCanonBatch([root], hostLabel: hostLabel).single;

/// [MontyValue]s -> canonical forms with one identity memo over the whole
/// batch, as the oracle's `canon_batch` does: a container, named tuple or
/// class instance reached more than once anywhere in [roots] (a call's
/// arguments and keyword arguments, say) is written once with an `id`, and
/// each later occurrence as `{"t": "ref", "id": n}`. The walk goes into class
/// instance attributes too. A class instance carries its `instance_id`.
///
/// Identity is Dart object identity: the worker driver decodes a node it
/// meets twice into one object. Never `==`, which walks a shared graph as a
/// tree.
List<Object?> identityCanonBatch(
  List<MontyValue> roots, {
  String? Function(MontyValue)? hostLabel,
}) {
  final counts = HashMap<MontyValue, int>.identity();
  final onPath = HashSet<MontyValue>.identity();
  void count(MontyValue v) {
    if (!_isShared(v) || hostLabel?.call(v) != null) return;
    final n = counts[v] = (counts[v] ?? 0) + 1;
    if (n > 1 || !onPath.add(v)) return;
    _children(v).forEach(count);
    onPath.remove(v);
  }

  roots.forEach(count);
  final ids = HashMap<MontyValue, int>.identity();
  Object? enc(MontyValue v) {
    if (hostLabel?.call(v) case final String label) {
      return {'t': 'hostobj', 'obj': label};
    }
    if (!_isShared(v)) return montyToCanon(v);
    if (ids[v] case final int id) return {'t': 'ref', 'id': id};
    final out = <String, Object?>{};
    if ((counts[v] ?? 0) > 1) out['id'] = ids[v] = ids.length + 1;
    switch (v) {
      case MontyList(:final items):
        out['t'] = 'list';
        out['items'] = [for (final i in items) enc(i)];
      case MontyTuple(:final items):
        out['t'] = 'tuple';
        out['items'] = [for (final i in items) enc(i)];
      case MontySet(:final items):
        out['t'] = 'set';
        out['items'] = sortedItems([for (final i in items) enc(i)]);
      case MontyFrozenSet(:final items):
        out['t'] = 'frozenset';
        out['items'] = sortedItems([for (final i in items) enc(i)]);
      case MontyDict(:final pairs):
        out['t'] = 'dict';
        out['items'] = [
          for (final (k, x) in pairs) [enc(k), enc(x)],
        ];
      case MontyNamedTuple(:final typeName, :final fieldNames, :final values):
        out['t'] = 'namedtuple';
        out['type'] = typeName;
        out['fields'] = fieldNames;
        out['items'] = [for (final i in values) enc(i)];
      case MontyClassProxy(:final classType, :final instanceId, :final attrs):
        out['t'] = 'proxy';
        out['class'] = classType.name;
        out['dataclass'] = classType.isDataclass;
        out['instance_id'] = instanceId;
        out['attrs'] = [
          for (final MapEntry(:key, :value) in attrs.entries) [key, enc(value)],
        ];
        out['_host_defined'] = classType.hostDefined;
      case MontyClassTypeProxy(
        :final name,
        :final isDataclass,
        :final id,
        :final attrs,
        :final hostDefined,
      ):
        out['t'] = 'typeproxy';
        out['name'] = name;
        out['dataclass'] = isDataclass;
        out['class_id'] = id;
        out['attrs'] = [
          for (final MapEntry(:key, :value) in attrs.entries) [key, enc(value)],
        ];
        out['_host_defined'] = hostDefined;
      default:
        throw StateError('not an identity-carrying value: ${v.runtimeType}');
    }

    return out;
  }

  return [for (final r in roots) enc(r)];
}

/// A raised error as the oracle records it (`exc_fields`): the message is
/// `display('msg')` and the type is what `display('type-msg')` puts before
/// it, which is the type upstream names even when the host raised the error
/// itself (an unpaired surrogate's `UnicodeEncodeError`, where `exception()`
/// reads as `ValueError`).
Map<String, Object?> raisedDisplayed(
  String Function([String format]) display,
  List<Frame> traceback, [
  Map<String, MontyValue> attributes = const {},
]) {
  final msg = display('msg');
  final typeMsg = display('type-msg');
  final type = msg.isNotEmpty && typeMsg.endsWith(': $msg')
      ? typeMsg.substring(0, typeMsg.length - msg.length - 2)
      : typeMsg.split(':').first;

  return {
    'type': type,
    'message': msg,
    'traceback': [for (final f in traceback) _frameC(f)],
    if (attributes.isNotEmpty) 'data': _excData(attributes),
  };
}

/// An exception's structured payload as the oracle's `exc_data` records it:
/// the kind (`unicode` when the attributes name an encoding, else `json`),
/// `object` in canonical form and the other attributes as plain ints and
/// strings.
Map<String, Object?> _excData(Map<String, MontyValue> attributes) => {
  'kind': attributes.containsKey('encoding') ? 'unicode' : 'json',
  for (final MapEntry(:key, :value) in attributes.entries)
    key: switch (value) {
      _ when key == 'object' => montyToCanon(value),
      MontyInt(value: final n) => n,
      MontyString(value: final text) => text,
      _ => montyToCanon(value),
    },
};

Map<String, Object?> _frameC(Frame f) => {
  'filename': f.filename,
  'line': f.line,
  'column': f.column,
  'end_line': f.endLine,
  'end_column': f.endColumn,
  'function': f.functionName,
  'source_line': f.sourceLine,
};
