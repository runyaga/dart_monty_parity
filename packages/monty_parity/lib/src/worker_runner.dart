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
import 'package:dart_monty_core/src/platform/exception_attributes.dart';
import 'package:dart_monty_core/src/worker/monty_channel.dart';
import 'package:dart_monty_core/src/worker/monty_error.dart';
import 'package:dart_monty_core/src/worker/monty_snapshot.dart';
import 'package:dart_monty_core/src/worker/os_policy.dart';
import 'package:dart_monty_core/src/worker/proto_session.dart';
import 'package:dart_monty_core/src/worker/resource_limits.dart';
import 'package:monty_parity/src/canon.dart';
import 'package:monty_parity/src/case_runner.dart';

/// Opens a fresh channel to a new worker.
typedef ChannelFactory = Future<MontyChannel> Function();

/// The case keys this engine has no way to apply. Each one present is
/// recorded under `unsupported` and the case runs without it.
const _inexpressible = {
  'cwd': 'no per-feed cwd in the worker driver; ran without it',
  'fs': 'no mounts in the worker driver; ran without them',
  'host': 'no host objects in the worker driver; ran without them',
  'restore_before': 'no snapshot/restore in the worker driver',
  'close_mount_before_feed': 'no mounts in the worker driver',
  'assert_message_annotations': 'no knob in the worker driver',
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
  });

  /// Ends the session.
  Future<void> close();
}

/// Opens a session configured as [config] says.
typedef OpenCaseSession = Future<CaseSession> Function(SessionConfig config);

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
  open: (config) async =>
      _ProtoCaseSession(await ProtoSession.open(await spawn(), config)),
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
}) async {
  final out = newOutcome(c['id']! as String, engine, backend);
  out['engine_version'] = engineVersion;
  final unsupported = out['unsupported']! as Map<String, Object?>;
  for (final MapEntry(:key, :value) in _inexpressible.entries) {
    if (c[key] != null) unsupported[key] = value;
  }
  final config = configure(c, unsupported);
  final host = CoreHost(c);
  // One memo: inputs may share objects with each other, as the oracle's do.
  final memo = <int, MontyValue>{};
  var inputs = const <String, MontyValue>{};
  Inexpressible? inexpressible;
  try {
    inputs = {
      for (final MapEntry(:key, :value)
          in ((c['inputs'] as Map?) ?? const {})
              .cast<String, Object?>()
              .entries)
        key: _input((value! as Map).cast(), host, memo),
    };
  } on Inexpressible catch (e) {
    // An input this runner cannot build yet (a file position an int cannot
    // hold): the first feed records it, so the case still has an outcome.
    inexpressible = e;
  }
  final script = Script(c);
  final steps = out['steps']! as List<Object?>;
  final CaseSession session;
  try {
    session = await open(config);
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
    final feeds =
        (c['feeds'] as List?)?.cast<String>() ?? [c['code']! as String];
    for (var n = 0; n < feeds.length; n++) {
      final step = newStep();
      steps.add(step);
      final prints = <(String, String)>[];
      final ext = step['ext_calls']! as List<Object?>;
      final os = step['os_calls']! as List<Object?>;
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
          feeds[n],
          inputs: n == 0 ? inputs : const {},
          externals: {
            for (final name in script.externals.keys)
              name: (args, kwargs) {
                ext.add(_callRecord(name, args, kwargs));

                return _answer(script.next(name, isOs: false), name, host);
              },
          },
          onOsCall: (name, args, kwargs) {
            os.add(_callRecord(name, args, kwargs));
            final a = script.next(name, isOs: true);
            if (a == null) throw const OsCallNotHandledException();

            return _answer(a, name, host);
          },
          onPrint: (stream, text) => prints.add((stream, text)),
        );
        step['value'] = identityCanon(value);
        host.rawOutputs.add(value);
      } on MontyRuntimeError catch (e) {
        step['exception'] = raisedDisplayed(
          e.display,
          e.traceback,
          exceptionAttributes(e.exception),
        );
        step['thrown'] = 'MontyRuntimeError';
        host.rawOutputs.add(null);
      } on MontySyntaxError catch (e) {
        step['exception'] = raisedDisplayed(
          e.display,
          e.traceback,
          exceptionAttributes(e.exception),
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
  }) async {
    final result = await _session.feed(
      code,
      inputs: inputs,
      externals: externals,
      onOsCall: onOsCall,
      onPrint: onPrint,
    );

    return result.value;
  }

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
  Duration? feed;
  final limits = ((c['limits'] as Map?) ?? const {}).cast<String, Object?>();
  for (final MapEntry(:key, :value) in limits.entries) {
    switch (key) {
      case 'max_memory':
        memory = value! as int;
      case 'max_recursion_depth':
        recursion = value! as int;
      case 'max_duration_secs' || 'max_feed_duration_secs':
        feed = Duration(microseconds: ((value! as num) * 1e6).round());
      default:
        unsupported['limits.$key'] = 'the worker driver has no $key';
    }
  }

  final osPolicy = _osPolicy(c, unsupported);

  // No limit set sends no limits, as upstream's checkout without `limits`.
  if (memory == null && recursion == null && feed == null) {
    return SessionConfig(osPolicy: osPolicy);
  }

  return SessionConfig(
    limits: ResourceLimits(
      maxMemory: memory,
      maxRecursionDepth: recursion,
      maxFeedDuration: feed,
    ),
    osPolicy: osPolicy,
  );
}

/// A case's `os_policy` as core's [OsPolicy], or null when it has none. A
/// form the case file spells but this reader does not build (a fixed
/// datetime, whose microseconds a web `DateTime` cannot hold) is recorded
/// under `unsupported`, and the policy is left out.
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
  if (p['datetime'] is Map) {
    unsupported['os_policy'] =
        'a fixed datetime: a web DateTime holds whole milliseconds';

    return null;
  }

  return OsPolicy(
    datetime: mode('datetime'),
    timezone: mode('timezone'),
    sleep: mode('sleep'),
    processTime: mode('process_time'),
    randomStart: mode('random_start'),
    randomSeed: seed,
  );
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

/// The canonical kinds the worker can carry that core's public API cannot:
/// a file handle, and a sandbox class instance the host names by id
/// (`instance_id`, `class_id`), which the worker resolves, or the driver
/// refuses when the id is not a canonical uuid.
MontyValue? _workerExtra(Map<String, Object?> c) => switch (c['t']) {
  'filehandle' => MontyFileHandle(
    path: c['path']! as String,
    mode: c['mode']! as String,
    position: _position(c['position']! as String),
  ),
  'proxy' => MontyClassInstance(
    classType: MontyClassType(
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

/// A file position as an `int`; one an `int` cannot hold exactly on this
/// compiler is inexpressible until the position is a `BigInt` (the cutover).
int _position(String text) {
  final b = BigInt.parse(text);
  if (!b.isValidInt) {
    throw Inexpressible('file position $text does not fit an int here');
  }

  return b.toInt();
}

/// Canonical input [c] -> [MontyValue], sharing [memo] across the inputs.
MontyValue _input(
  Map<String, Object?> c,
  CoreHost host,
  Map<int, MontyValue> memo,
) => canonToMonty(c, host, memo, _workerExtra);

/// A scripted answer [a] for the call to [name], as the driver takes it.
MontyValue _answer(Map<String, Object?>? a, String name, CoreHost host) {
  if (a == null) {
    throw ExternalException('NameError', "name '$name' is not defined");
  }
  if (a['return'] case final Map<Object?, Object?> ret) {
    return canonToMonty(ret.cast(), host, null, _workerExtra);
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
    v is MontyClassInstance;

List<MontyValue> _children(MontyValue v) => switch (v) {
  MontyList(:final items) || MontyTuple(:final items) => items,
  MontySet(:final items) || MontyFrozenSet(:final items) => items,
  MontyDict(:final pairs) => [
    for (final (k, x) in pairs) ...[k, x],
  ],
  MontyNamedTuple(:final values) => values,
  MontyClassInstance(:final attrs) => [...attrs.values],
  _ => const [],
};

/// [MontyValue] -> canonical form, identity-aware as the oracle's `canon` is.
Object? identityCanon(MontyValue root) => identityCanonBatch([root]).single;

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
List<Object?> identityCanonBatch(List<MontyValue> roots) {
  final counts = HashMap<MontyValue, int>.identity();
  final onPath = HashSet<MontyValue>.identity();
  void count(MontyValue v) {
    if (!_isShared(v)) return;
    final n = counts[v] = (counts[v] ?? 0) + 1;
    if (n > 1 || !onPath.add(v)) return;
    _children(v).forEach(count);
    onPath.remove(v);
  }

  roots.forEach(count);
  final ids = HashMap<MontyValue, int>.identity();
  Object? enc(MontyValue v) {
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
      case MontyClassInstance(
        :final classType,
        :final instanceId,
        :final attrs,
      ):
        out['t'] = 'proxy';
        out['class'] = classType.name;
        out['dataclass'] = classType.isDataclass;
        out['instance_id'] = instanceId;
        out['attrs'] = [
          for (final MapEntry(:key, :value) in attrs.entries) [key, enc(value)],
        ];
        out['_host_defined'] = classType.hostDefined;
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
