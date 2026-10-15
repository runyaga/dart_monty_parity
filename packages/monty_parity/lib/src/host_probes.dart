// The host-object probes (`sp_host_*`) of the session engine: each case of
// tool/parity/oracle/host_cases.py (generated into host_cases.g.dart) run
// through core's internal pool session with core's host-object wrappers
// (`ClassType`, `ClassInstance`, `HostClass`). Each step records one `host`
// event, the oracle's (tool/parity/oracle/run_probes.py `host_probe`): the
// step as JSON and its outcome as text. Web-safe.
//
// The host class is `Point`, mirrored from host_cases.py.
// ignore_for_file: implementation_imports
import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:dart_monty_core/dart_monty_core.dart'
    hide MontyError, MontySyntaxError, MontyTypingError;
import 'package:dart_monty_core/src/worker/host_objects.dart';
import 'package:dart_monty_core/src/worker/worker_pool.dart';
import 'package:monty_parity/src/host_cases.g.dart';

/// Starts a pool with the given options.
typedef StartHostPool = Future<Monty> Function(PoolOptions options);

/// Records one event of the running probe.
typedef HostEmit = void Function(Map<String, Object?> event);

/// The ids of the host probes, `sp_host_<case id>`.
final Set<String> hostProbeIds = {
  for (final c in _cases) 'sp_host_${c['id']}',
};

final List<Map<String, Object?>> _cases = [
  for (final c in jsonDecode(hostCasesJson) as List<Object?>)
    (c! as Map).cast<String, Object?>(),
];

/// Runs host probe [id], emitting its events through [emit].
Future<void> runHostProbe(
  String id,
  StartHostPool start,
  HostEmit emit,
) async {
  final c = _cases.firstWhere((c) => 'sp_host_${c['id']}' == id);
  final pool = await start(const PoolOptions(maxProcesses: 2));
  try {
    final objects = {
      for (final MapEntry(:key, :value)
          in (c['objects']! as Map).cast<String, Object?>().entries)
        key: switch (value) {
          [final int x, final int y] => Point(x, y),
          _ => throw StateError('$value'),
        },
    };
    final run = _Run(pool, objects, _build(objects, c['wrappers']! as List));
    try {
      for (final step in (c['steps']! as List).cast<List<Object?>>()) {
        emit({
          'ev': 'host',
          'step': jsonEncode(step),
          'outcome': await run.step(step),
        });
      }
    } finally {
      await run.closeAll();
    }
  } finally {
    await pool.close();
  }
}

void _ignore(String stream, String text) {}

/// The oracle's `Point`.
final class Point {
  /// A point at ([x], [y]).
  const Point(this.x, this.y);

  /// The abscissa.
  final int x;

  /// The ordinate.
  final int y;
}

int _int(List<MontyValue> args, int i) => (args[i] as MontyInt).value;

Point _construct(List<MontyValue> args, Map<String, MontyValue> kwargs) =>
    switch (args) {
      [MontyInt(value: final x), MontyInt(value: final y)] => Point(x, y),
      _ => throw const ExternalException(
        'TypeError',
        "Point.__init__() missing 1 required positional argument: 'y'",
      ),
    };

FutureOr<Object> _describe(List<MontyValue> args, Map<String, MontyValue> kw) =>
    const MontyString('Point class');

FutureOr<Object> _add(List<MontyValue> args, Map<String, MontyValue> kw) =>
    MontyInt(_int(args, 0) + _int(args, 1));

/// `Point`'s members, as host_cases.py's class defines them.
final HostClass<Point> pointClass = HostClass(
  'Point',
  isDataclass: true,
  instance: (
    fields: {'x': (p) => MontyInt(p.x), 'y': (p) => MontyInt(p.y)},
    properties: {
      'norm1': (p) => MontyInt(p.x.abs() + p.y.abs()),
      'boom': (p) => throw const ExternalException('KeyError', 'boom'),
      'missing': (p) =>
          throw const ExternalException('AttributeError', 'missing'),
    },
    methods: {
      'sum': (p, args, kwargs) => MontyInt(p.x + p.y),
      'shifted': (p, args, kwargs) => MontyList([
        MontyInt(p.x + _int(args, 0)),
        MontyInt(
          p.y +
              switch (kwargs['dy']) {
                MontyInt(:final value) => value,
                _ => 0,
              },
        ),
      ]),
      'later': (p, args, kwargs) => Future.value(MontyInt(p.x * 10)),
      'fails': (p, args, kwargs) =>
          throw const ExternalException('ValueError', 'no sum today'),
    },
  ),
  attributes: {'ORIGIN_NAME': () => const MontyString('origin')},
  methods: {'describe': _describe, 'add': _add},
  construct: _construct,
);

/// A policy as the cases spell it: null, 'all', or a list of names.
Object? _policy(Object? spec) =>
    spec is List<Object?> ? spec.cast<String>().toList() : spec;

typedef _Policies = ({
  Object? eagerAttrs,
  Object? lazyAttrs,
  Object? allowedMethods,
});

_Policies _policies(Map<String, Object?> map) => (
  eagerAttrs: _policy(map['eager']),
  lazyAttrs: _policy(map['lazy']),
  allowedMethods: _policy(map['methods']),
);

String? _id(Map<String, Object?> options) => switch (options['id']) {
  final int n =>
    '00000000-0000-0000-0000-${n.toRadixString(16).padLeft(12, '0')}',
  _ => null,
};

Object _redact(String name, Object value) =>
    name == 'x' ? const MontyString('***') : value;

/// The wrappers a case builds, in order, by name.
Map<String, Object> _build(Map<String, Point> objects, List<Object?> specs) {
  final built = <String, Object>{};
  for (final entry in specs) {
    if (entry case [final String name, final List<Object?> spec]) {
      built[name] = _wrapper(spec, built, objects);
    }
  }

  return built;
}

Object _wrapper(
  List<Object?> spec,
  Map<String, Object> built,
  Map<String, Point> objects,
) => switch (spec) {
  [
    'T',
    final Map<String, Object?> policies,
    final Map<String, Object?> options,
  ] =>
    ClassType(
      pointClass,
      id: _id(options),
      policies: _policies(policies),
      init: options['init'] == true,
      instancePolicies: switch (options['instance']) {
        final Map<String, Object?> instance => _policies(instance),
        _ => (eagerAttrs: null, lazyAttrs: null, allowedMethods: null),
      },
      convertValue: options['convert'] == 'redact' ? _redact : null,
    ),
  [
    'I',
    final String object,
    final Map<String, Object?> policies,
    final Map<String, Object?> options,
  ] =>
    ClassInstance(
      objects[object] ?? (throw StateError('no object $object')),
      classType: switch (built[options['class']]) {
        final ClassType<Point> type => type,
        _ => ClassType(pointClass),
      },
      id: _id(options),
      policies: _policies(policies),
    ),
  _ => throw StateError('bad wrapper spec $spec'),
};

/// [s] as Python's `repr` writes a `str`.
String _strRepr(String s) {
  final quote = s.contains("'") && !s.contains('"') ? '"' : "'";
  final body = s.replaceAll(r'\', r'\\').replaceAll('\n', r'\n');

  return '$quote${quote == "'" ? body.replaceAll("'", r"\'") : body}$quote';
}

/// [v] in the probes' form: a host object is `is:<name>`, a proxy
/// `proxy:<class>`, else Python's repr.
String _show(MontyValue v, Map<String, Point> objects) {
  if (v case ClassInstance(:final value)) {
    for (final MapEntry(:key, value: object) in objects.entries) {
      if (identical(object, value)) return 'is:$key';
    }
  }

  return switch (v) {
    MontyClassProxy(:final classType) => 'proxy:${classType.name}',
    MontyList(:final items) =>
      '[${items.map((i) => _show(i, objects)).join(', ')}]',
    MontyTuple(items: [final only]) => '(${_show(only, objects)},)',
    MontyTuple(:final items) =>
      '(${items.map((i) => _show(i, objects)).join(', ')})',
    MontyString(:final value) => _strRepr(value),
    MontyBool(:final value) => value ? 'True' : 'False',
    MontyNone() => 'None',
    MontyInt(:final value) => '$value',
    _ => '$v',
  };
}

String _errorText(Object error) => switch (error) {
  MontyRuntimeError() => "MontyRuntimeError: ${error.display('type-msg')}",
  _ => '${error.runtimeType}: $error',
};

/// One case's sessions and dumps.
final class _Run {
  _Run(this._pool, this._objects, this._wrappers);

  final Monty _pool;
  final Map<String, Point> _objects;
  final Map<String, Object> _wrappers;
  final Map<String, MontySession> _sessions = {};
  final Map<String, Uint8List> _dumps = {};

  Future<void> closeAll() async {
    for (final session in _sessions.values) {
      await session.close();
    }
  }

  Future<String> step(List<Object?> step) async {
    try {
      return await _step(step);
    } on Object catch (error) {
      return _errorText(error);
    }
  }

  Future<String> _step(List<Object?> step) async {
    final [op as String, name as String, ...rest] = step;
    switch (op) {
      case 'open':
        _sessions[name] = await _pool.checkout();

        return 'ok';
      case 'feed':
        final [code as String, inputs as List<Object?>] = rest;
        final value = await _sessions[name]!.feedRun(
          code,
          inputs: {for (final n in inputs.cast<String>()) n: _wrappers[n]!},
          printCallback: _ignore,
        );

        return 'value: ${_show(value, _objects)}';
      case 'dump':
        _dumps[name] = await _sessions[name]!.dump();

        return 'dumped';
      case 'load_session':
        await _sessions[name]!.loadSession(_dumps[rest.single]!);

        return 'ok';
    }
    throw StateError('unknown step $op');
  }
}
