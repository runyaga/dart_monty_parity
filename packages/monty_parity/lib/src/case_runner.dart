// The value conversions and the case's host world shared by every engine:
// canonical form <-> [MontyValue], and a case's `fs` block as core's
// memory-mounted handler. The engines themselves are worker_runner.dart
// (the raw worker) and session_runner.dart (`Monty` / `MontySession`, the
// `core` and `session` engines). Web-safe.
//
// The internal factories build the proxy values core's public API decodes
// but has no public constructor for.
// ignore_for_file: implementation_imports
import 'dart:typed_data';

import 'package:dart_monty_core/dart_monty_core.dart';
import 'package:dart_monty_core/src/mount/vfs_limits.dart'
    show defaultMemoryUsageLimit;
import 'package:dart_monty_core/src/platform/monty_value.dart'
    show montyClassProxy, montyClassTypeProxy, montyStdTypeProxy;
import 'package:monty_parity/src/canon.dart';

/// A canonical value core has no way to express.
final class Inexpressible implements Exception {
  /// Creates an [Inexpressible] with [message].
  const Inexpressible(this.message);

  /// What could not be expressed.
  final String message;

  @override
  String toString() => 'Unsupported operation: $message';
}

// ------------------------------------------------------------- values out

/// [MontyValue] -> canonical form.
Object? montyToCanon(MontyValue v) => switch (v) {
  MontyNone() => {'t': 'none'},
  MontyBool(:final value) => {'t': 'bool', 'v': value},
  MontyInt(:final value) => {'t': 'int', 'v': '$value'},
  MontyBigInt(:final value) => {'t': 'int', 'v': '$value'},
  MontyFloat(:final value) => canonFloat(value),
  MontyComplex(:final real, :final imag) => {
    't': 'complex',
    're': canonFloat(real),
    'im': canonFloat(imag),
  },
  MontyString(:final value) => {'t': 'str', 'v': value},
  MontyBytes(:final value) => {'t': 'bytes', 'hex': hexOf(value)},
  MontyList(:final items) => {
    't': 'list',
    'items': [for (final i in items) montyToCanon(i)],
  },
  MontyTuple(:final items) => {
    't': 'tuple',
    'items': [for (final i in items) montyToCanon(i)],
  },
  MontySet(:final items) => {
    't': 'set',
    'items': sortedItems([for (final i in items) montyToCanon(i)]),
  },
  MontyFrozenSet(:final items) => {
    't': 'frozenset',
    'items': sortedItems([for (final i in items) montyToCanon(i)]),
  },
  MontyDict(:final pairs) => {
    't': 'dict',
    'items': [
      for (final (k, x) in pairs) [montyToCanon(k), montyToCanon(x)],
    ],
  },
  MontyPath(:final value) => {'t': 'path', 'v': value},
  MontyEllipsis() => {'t': 'ellipsis'},
  MontyNotImplemented() => {'t': 'notimpl'},
  MontyNamedTuple(:final typeName, :final fieldNames, :final values) => {
    't': 'namedtuple',
    'type': typeName,
    'fields': fieldNames,
    'items': [for (final i in values) montyToCanon(i)],
  },
  MontyFileHandle(:final path, :final mode, :final position) => {
    't': 'filehandle',
    'path': path,
    'mode': mode,
    'position': '$position',
  },
  MontyDate(:final year, :final month, :final day) => {
    't': 'date',
    'v': '${_p(year, 4)}-${_p(month)}-${_p(day)}',
  },
  final MontyDateTime d => {
    't': 'datetime',
    'v':
        '${_p(d.year, 4)}-${_p(d.month)}-${_p(d.day)}'
        'T${_hms(d.hour, d.minute, d.second, d.microsecond)}',
    'offset': d.offsetSeconds,
    'tzname': d.timezoneName,
  },
  final MontyTime d => {
    't': 'time',
    'v': _hms(d.hour, d.minute, d.second, d.microsecond),
    'offset': d.offsetSeconds,
    'tzname': d.timezoneName,
    'fold': d.fold,
  },
  MontyTimeDelta(:final days, :final seconds, :final microseconds) => {
    't': 'timedelta',
    'days': days,
    'seconds': seconds,
    'us': microseconds,
  },
  MontyTimeZone(:final offsetSeconds, :final name) => {
    't': 'timezone',
    'offset': offsetSeconds,
    'name': name,
  },
  MontyExceptionValue(:final excType, :final message) => {
    't': 'exception',
    'type': excType,
    'arg': message,
  },
  final MontyClassProxy ci =>
    _hostLabels[ci.instanceId] != null
        ? {'t': 'hostobj', 'obj': _hostLabels[ci.instanceId]}
        : {
            't': 'proxy',
            'class': ci.classType.name,
            'dataclass': ci.classType.isDataclass,
            'attrs': [
              for (final e in ci.attrs.entries) [e.key, montyToCanon(e.value)],
            ],
            '_host_defined': ci.classType.hostDefined,
          },
  final MontyClassTypeProxy ct => {
    't': 'typeproxy',
    'name': ct.name,
    'dataclass': ct.isDataclass,
    'class_id': ct.id,
    'attrs': [
      for (final e in ct.attrs.entries) [e.key, montyToCanon(e.value)],
    ],
    '_host_defined': ct.hostDefined,
  },
  // A registered host object or class decodes to its wrapper; the oracle
  // writes it by its label (an instance) or its class name.
  ClassInstance(:final id) =>
    _hostLabels[id] != null
        ? {'t': 'hostobj', 'obj': _hostLabels[id]}
        : {'t': 'other', 'kind': 'ClassInstance', 'repr': '$v'},
  ClassType(:final value) => {'t': 'hostclass', 'obj': value.name},
  MontyHostFunction() => throw StateError(
    'unreachable: MontyHostFunction is input-only',
  ),
  // Upstream's binding returns builtin functions as MontyStdTypeProxy and
  // type objects as types; core's proxy names a builtin function's kind
  // `function`.
  MontyStdTypeProxy(:final kind, :final name) => switch (kind) {
    'function' || 'builtin' => {'t': 'builtin', 'name': name},
    'type' => {'t': 'type', 'name': name},
    _ => {'t': 'opaque', 'kind': kind, 'v': name},
  },
};

String _p(int n, [int w = 2]) => n.toString().padLeft(w, '0');
String _hms(int h, int m, int s, int us) =>
    '${_p(h)}:${_p(m)}:${_p(s)}.${_p(us, 6)}';

/// Host-object labels: instanceId -> the case's label for it.
final _hostLabels = <String, String>{};

/// Labels the host object whose sandbox id is [id] as [label].
void labelHostObject(String id, String label) => _hostLabels[id] = label;

/// Callback args arrive as plain Dart values (or MontyValues).
Object? dartToCanon(Object? v) {
  _argTypes.add(v.runtimeType.toString());
  return _dartToCanon(v);
}

/// Runtime types seen in callback args (evidence for the bytes/path
/// projection), reported per case under `notes.os_arg_runtime_types`.
final _argTypes = <String>{};

Object? _dartToCanon(Object? v) => switch (v) {
  null => {'t': 'none'},
  final MontyValue m => montyToCanon(m),
  final bool b => {'t': 'bool', 'v': b},
  final int i => {'t': 'int', 'v': '$i'},
  final double d => canonFloat(d),
  final BigInt b => {'t': 'int', 'v': '$b'},
  final String s => {'t': 'str', 'v': s},
  final Uint8List b => {'t': 'bytes', 'hex': hexOf(b)},
  final List<Object?> l => {
    't': 'list',
    'items': [for (final x in l) dartToCanon(x)],
  },
  final Map<Object?, Object?> m => {
    't': 'dict',
    'items': [
      for (final e in m.entries) [dartToCanon(e.key), dartToCanon(e.value)],
    ],
  },
  _ => {'t': 'other', 'kind': v.runtimeType.toString(), 'repr': '$v'},
};

// -------------------------------------------------------------- values in

/// Canonical kinds an engine builds itself, before [canonToMonty]'s own; null
/// for any other kind.
typedef CanonExtra = MontyValue? Function(Map<String, Object?> c);

/// Canonical -> [MontyValue] for callback returns (core's typed host->Python
/// path). Shared `id`s map to the same Dart object; core still serialises a
/// tree. [extra] is asked first at every level.
MontyValue canonToMonty(
  Map<String, Object?> c,
  CoreHost host, [
  Map<int, MontyValue>? memo,
  CanonExtra? extra,
]) {
  memo ??= {};
  final t = c['t'];
  if (t == 'ref') return memo[c['id']]!;
  if (extra?.call(c) case final MontyValue v) {
    if (c['id'] case final int id) memo[id] = v;

    return v;
  }
  List<MontyValue> items() => [
    for (final x in c['items']! as List)
      canonToMonty((x! as Map).cast(), host, memo, extra),
  ];
  (int, int, int, int) hms(String v) {
    final p = v.split(RegExp('[:.]'));
    return (
      int.parse(p[0]),
      int.parse(p[1]),
      int.parse(p[2]),
      p.length > 3 ? int.parse(p[3].padRight(6, '0')) : 0,
    );
  }

  final v = switch (t) {
    'none' => const MontyNone(),
    'ellipsis' => const MontyEllipsis(),
    'notimpl' => const MontyNotImplemented(),
    'bool' => MontyBool(c['v']! as bool),
    'int' => () {
      final b = BigInt.parse(c['v']! as String);
      return b.isValidInt ? MontyInt(b.toInt()) : MontyBigInt(b);
    }(),
    'float' => MontyFloat(decodeFloat(c)),
    'complex' => MontyComplex(
      decodeFloat((c['re']! as Map).cast()),
      decodeFloat((c['im']! as Map).cast()),
    ),
    'str' => MontyString(c['v']! as String),
    'bytes' => MontyBytes(Uint8List.fromList(bytesOfHex(c['hex']! as String))),
    'list' => () {
      // Registered before its items are read, so an item may refer back to
      // it: a cyclic list, which a host may build and the writer refuses.
      final out = <MontyValue>[];
      final list = MontyList(out);
      if (c['id'] case final int id) memo![id] = list;
      out.addAll(items());

      return list;
    }(),
    'tuple' => MontyTuple(items()),
    'set' => MontySet(items()),
    'frozenset' => MontyFrozenSet(items()),
    'dict' => MontyDict([
      for (final kv in c['items']! as List)
        (
          canonToMonty(((kv as List)[0] as Map).cast(), host, memo, extra),
          canonToMonty((kv[1] as Map).cast(), host, memo, extra),
        ),
    ]),
    'namedtuple' => MontyNamedTuple(
      typeName: c['type']! as String,
      fieldNames: (c['fields']! as List).cast<String>(),
      values: items(),
    ),
    'path' => MontyPath(c['v']! as String),
    'date' => () {
      final p = (c['v']! as String).split('-').map(int.parse).toList();
      return MontyDate(year: p[0], month: p[1], day: p[2]);
    }(),
    'datetime' => () {
      // A value spelled with its offset ("...+00:00") and no `offset` key
      // carries the offset in the text.
      final text = c['v']! as String;
      final tail = RegExp(r'([+-])(\d\d):(\d\d)$').firstMatch(text);
      final inText = c.containsKey('offset') || tail == null
          ? null
          : (tail[1] == '-' ? -1 : 1) *
                (int.parse(tail[2]!) * 3600 + int.parse(tail[3]!) * 60);
      final s =
          (tail == null || inText == null
                  ? text
                  : text.substring(0, tail.start))
              .split('T');
      final d = s[0].split('-').map(int.parse).toList();
      final (h, m, sec, us) = hms(s[1]);
      return MontyDateTime(
        year: d[0],
        month: d[1],
        day: d[2],
        hour: h,
        minute: m,
        second: sec,
        microsecond: us,
        offsetSeconds: (c['offset'] as int?) ?? inText,
        timezoneName: c['tzname'] as String?,
      );
    }(),
    'time' => () {
      final (h, m, sec, us) = hms(c['v']! as String);
      return MontyTime(
        hour: h,
        minute: m,
        second: sec,
        microsecond: us,
        offsetSeconds: c['offset'] as int?,
        timezoneName: c['tzname'] as String?,
        fold: (c['fold'] as int?) ?? 0,
      );
    }(),
    'timedelta' => MontyTimeDelta(
      days: c['days']! as int,
      seconds: c['seconds']! as int,
      microseconds: c['us']! as int,
    ),
    'timezone' => MontyTimeZone(
      offsetSeconds: c['offset']! as int,
      name: c['name'] as String?,
    ),
    'exception' => MontyExceptionValue(
      c['type']! as String,
      c['arg'] as String?,
    ),
    'type' =>
      host.internal('a type proxy') ??
          montyStdTypeProxy('type', c['name']! as String),
    'builtin' =>
      host.internal('a builtin proxy') ??
          montyStdTypeProxy('function', c['name']! as String),
    'echo' => host.rawOutputs[c['step']! as int]!,
    'wrap' =>
      host.internal('a host-object proxy') ??
          host.wrapperValue(c['w']! as String),
    'proxy' => throw const Inexpressible(
      'core: no way to rebuild a proxy from canonical',
    ),
    final other => throw Inexpressible(
      'core cannot express canonical "$other"',
    ),
  };
  if (c['id'] case final int id) memo[id] = v;
  return v;
}

// ---------------------------------------------------------- host objects

/// User-level host-object emulation through core's public API: a host
/// instance is a [MontyClassProxy] the host builds (eager attributes
/// only); method calls arrive as plain external calls by method name, so the
/// receiver is unknown. No policies, lazy attributes, class values or
/// conversion hooks.
class CoreHost {
  /// Builds the host world a case's `host` block describes.
  CoreHost(Map<String, Object?> c, {this.publicOnly = false}) {
    final host = ((c['host'] as Map?) ?? const {}).cast<String, Object?>();
    classes = ((host['classes'] as Map?) ?? const {}).cast<String, Object?>();
    objects = ((host['objects'] as Map?) ?? const {}).cast<String, Object?>();
    wspecs = ((host['wrappers'] as Map?) ?? const {}).cast<String, Object?>();
  }

  /// Whether the engine builds values only through core's public API (the
  /// `core` engine): a value only an internal factory makes is then
  /// inexpressible.
  final bool publicOnly;

  /// Throws [Inexpressible] for [what] when [publicOnly]; else null.
  Never? internal(String what) => publicOnly
      ? throw Inexpressible("core's public API cannot construct $what")
      : null;

  /// Class specs by name.
  late final Map<String, Object?> classes;

  /// Object specs by label.
  late final Map<String, Object?> objects;

  /// Wrapper specs by name.
  late final Map<String, Object?> wspecs;

  /// Whether this host builds the case's `host` block (core's public API
  /// does not; the session engine's does).
  bool get appliesHost => false;

  /// The label of the host object a decoded value stands for, or null.
  /// Core's public API has no such object; the session engine's host does.
  String? labelOf(MontyValue v) => null;

  /// Each step's raw value (for `echo` answers).
  final rawOutputs = <MontyValue?>[];

  final _granted = <String>[];

  /// The current step's host-call transcript.
  List<Object?> log = [];

  static int _n = 0;

  String _uuid(String seed) {
    final h = seed.hashCode.toUnsigned(32).toRadixString(16).padLeft(8, '0');
    final n = (++_n).toRadixString(16).padLeft(12, '0');
    return '$h-0000-4000-8000-$n';
  }

  /// The value a `wrap` canonical stands for.
  MontyValue wrapperValue(String name) {
    final ws = (wspecs[name]! as Map).cast<String, Object?>();
    if (ws['kind'] != 'instance') {
      throw const Inexpressible(
        'core: this engine builds no host class value',
      );
    }
    final label = ws['obj']! as String;
    final o = (objects[label]! as Map).cast<String, Object?>();
    final cname = o['class']! as String;
    final cls = (classes[cname]! as Map).cast<String, Object?>();
    final values = (o['values']! as Map).cast<String, Object?>();
    final eager = ws['eager'];
    final names = eager == null
        ? <String>[]
        : eager == 'all'
        ? [
            for (final k in values.keys)
              if (!k.startsWith('_')) k,
          ]
        : (eager as List).cast<String>();
    final iid = _uuid(label);
    _hostLabels[iid] = label;
    _granted.add(label);
    final classId = cname.hashCode
        .toUnsigned(32)
        .toRadixString(16)
        .padLeft(12, '0');
    return montyClassProxy(
      classType: montyClassTypeProxy(
        name: cname,
        id: '00000000-0000-4000-8000-$classId',
        hostDefined: true,
        isDataclass: cls['dataclass'] == true,
        attrs: const {},
      ),
      instanceId: iid,
      attrs: {
        for (final n in names)
          n: canonToMonty((values[n]! as Map).cast(), this),
      },
    );
  }
}

// ------------------------------------------------------------ filesystem

/// Builds core's memory-mounted handler from a case `fs` spec (mounts and
/// seeded files). Throws whatever core throws at construction.
OsHandler buildFsHandler(
  Map<String, Object?> fs,
  OsHandler fallthrough,
  Map<String, Object?> unsupported,
) {
  final mounts = <MountDir>[];
  for (final m in (fs['mounts']! as List).cast<Map<String, Object?>>()) {
    final mode = m['mode'] as String? ?? 'read-write';
    if (mode == 'overlay') {
      unsupported['fs.mode'] =
          'core MountMode has no overlay; ran as readWrite';
    }
    mounts.add(
      MountDir(
        virtualPath: m['virtual']! as String,
        mode: mode == 'read-only' ? MountMode.readOnly : MountMode.readWrite,
        writeBytesLimit: m['write_bytes_limit'] as int?,
        memoryUsageLimit:
            (m['memory_usage_limit'] as int?) ?? defaultMemoryUsageLimit,
      ),
    );
  }
  final files = <VfsFile>[];
  for (final e in ((fs['files'] as Map?) ?? const {}).entries) {
    final k = e.key as String;
    if (k.endsWith('/')) {
      unsupported['fs.empty_dir_seed'] =
          'core seeds directories only through files';
      continue;
    }
    final v = (e.value as Map).cast<String, Object?>();
    files.add(
      v.containsKey('hex')
          ? MontyMemoryFile(
              k,
              Uint8List.fromList(bytesOfHex(v['hex']! as String)),
            )
          : MontyMemoryFile(k, v['text']! as String),
    );
  }
  return memoryMountedOsHandler(
    mounts: mounts,
    files: files,
    fallthrough: fallthrough,
  );
}
