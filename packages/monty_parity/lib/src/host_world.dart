// The session engine's host world: a case's `host` block (classes, objects,
// wrappers; see tool/parity/oracle/run_cases.py `Ctx`) built from core's own
// host-object wrappers (`HostClass`, `ClassType`, `ClassInstance`), so the
// case runs through core's pool session as upstream's does through
// pydantic-monty. Web-safe.
//
// The oracle also logs what its Python subclasses see: every `convert_value`,
// and every `call_method` and `lookup_lazy_attrs` the sandbox reaches the
// wrapper with. Core's wrappers are `final` classes with no hook for the
// last two before the policy check, so a call or lookup the policy refuses
// reaches no host code here and leaves no `call` or `lookup` entry: the cases
// that need one cannot match (see `not_run` in rows.json).
import 'dart:async';

import 'package:dart_monty_core/dart_monty_core.dart'
    hide MontyError, MontySyntaxError, MontyTypingError;
import 'package:monty_parity/src/case_runner.dart';

/// One host object: an instance of a case's class, labelled as the oracle
/// labels it (`p1`, or `new:Person#1` when the sandbox built it).
final class HostObj {
  /// An instance of class [cls] holding [values], labelled [label].
  HostObj(this.cls, this.values, this.label);

  /// The class name.
  final String cls;

  /// The instance's data by field.
  final Map<String, MontyValue> values;

  /// The oracle's label for it.
  final String label;
}

/// The host world of a session-engine case.
final class SessionHost extends CoreHost {
  /// Builds the world [c]'s `host` block describes.
  SessionHost(super.c, {super.publicOnly}) {
    for (final MapEntry(:key, :value) in classes.entries) {
      _classes[key] = _makeClass(key, (value! as Map).cast());
    }
    for (final MapEntry(:key, :value) in objects.entries) {
      final spec = (value! as Map).cast<String, Object?>();
      _objects[key] = HostObj(spec['class']! as String, {
        for (final MapEntry(:key, :value)
            in (spec['values']! as Map).cast<String, Object?>().entries)
          key: canonToMonty((value! as Map).cast(), this),
      }, key);
    }
    _logging = true;
  }

  final _classes = <String, HostClass<HostObj>>{};
  final _objects = <String, HostObj>{};
  final _wrappers = <String, MontyValue>{};
  final _newCount = <String, int>{};
  var _logging = false;

  @override
  bool get appliesHost => true;

  /// Whether the case has a host world at all.
  bool get active =>
      classes.isNotEmpty || objects.isNotEmpty || wspecs.isNotEmpty;

  @override
  MontyValue wrapperValue(String name) => _wrapper(name);

  /// The label of the host object [v] decodes to, if it is one.
  @override
  String? labelOf(MontyValue v) => switch (v) {
    ClassInstance(value: final HostObj o) => o.label,
    _ => null,
  };

  Map<String, Object?> _canon(Object? v) => switch (v) {
    final HostObj o => {'t': 'hostobj', 'obj': o.label},
    null => {'t': 'none'},
    final MontyValue m => montyToCanon(m)! as Map<String, Object?>,
    _ => throw StateError('no canonical form for $v'),
  };

  HostObj _new(String cname, Map<String, MontyValue> values) {
    final n = _newCount[cname] = (_newCount[cname] ?? 0) + 1;
    final o = HostObj(cname, values, 'new:$cname#$n');
    if (_logging) {
      log.add({
        'op': 'init',
        'class': cname,
        'obj': o.label,
        'attrs': [
          for (final MapEntry(:key, :value) in values.entries)
            [key, _canon(value)],
        ],
      });
    }

    return o;
  }

  HostClass<HostObj> _makeClass(String cname, Map<String, Object?> spec) {
    final fields = ((spec['fields'] as List?) ?? const []).cast<String>();
    final methods = ((spec['methods'] as Map?) ?? const {})
        .cast<String, Object?>();
    final instance =
        <
          String,
          FutureOr<Object> Function(
            HostObj,
            List<MontyValue>,
            Map<String, MontyValue>,
          )
        >{};
    final classLevel =
        <
          String,
          FutureOr<Object> Function(
            List<MontyValue>,
            Map<String, MontyValue>,
          )
        >{};
    for (final MapEntry(key: mname, :value) in methods.entries) {
      final beh = (value! as Map).cast<String, Object?>();
      switch (beh['kind']) {
        case 'static':
          classLevel[mname] = (args, kwargs) =>
              _invoke(null, cname, mname, beh, args, kwargs, fields);
        case 'classmethod':
          classLevel[mname] = (args, kwargs) =>
              _invoke(cname, cname, mname, beh, args, kwargs, fields);
        default:
          instance[mname] = (self, args, kwargs) =>
              _invoke(self, cname, mname, beh, args, kwargs, fields);
      }
    }
    final attrs = ((spec['class_attrs'] as Map?) ?? const {})
        .cast<String, Object?>();

    return HostClass<HostObj>(
      cname,
      isDataclass: spec['dataclass'] == true,
      instance: (
        fields: {for (final f in fields) f: (o) => o.values[f]!},
        properties: const {},
        methods: instance,
      ),
      attributes: {
        for (final MapEntry(:key, :value) in attrs.entries)
          key: () => canonToMonty((value! as Map).cast(), this),
      },
      methods: classLevel,
      construct: (args, kwargs) {
        log.add({
          'op': 'call',
          'obj': {'t': 'hostclass', 'obj': cname},
          'name': '__call__',
        });
        final values = <String, MontyValue>{};
        for (var i = 0; i < args.length && i < fields.length; i++) {
          values[fields[i]] = args[i];
        }
        values.addAll(kwargs);

        return _new(cname, values);
      },
    );
  }

  /// A method's behaviour; [receiver] is the instance, the class name for a
  /// classmethod, or null for a staticmethod.
  Object _invoke(
    Object? receiver,
    String cname,
    String mname,
    Map<String, Object?> beh,
    List<MontyValue> args,
    Map<String, MontyValue> kwargs,
    List<String> fields,
  ) {
    final self = receiver is HostObj ? receiver : null;
    final receiverCanon = switch (receiver) {
      final HostObj o => _canon(o),
      final String c => {'t': 'hostclass', 'obj': c},
      _ => {'t': 'none'},
    };
    log
      ..add({
        'op': 'call',
        'obj': switch (receiver) {
          final HostObj o => _canon(o),
          _ => {'t': 'hostclass', 'obj': cname},
        },
        'name': mname,
      })
      ..add({
        'op': 'method',
        'obj': receiverCanon,
        'name': mname,
        'args': [for (final a in args) _canon(a)],
        'kwargs': {
          for (final MapEntry(:key, :value) in kwargs.entries)
            key: _canon(value),
        },
      });
    if (beh['returns'] case final Map<Object?, Object?> r) {
      return canonToMonty(r.cast(), this);
    }
    if (beh['format'] case final String f) {
      var s = f;
      for (var i = 0; i < args.length; i++) {
        s = s.replaceAll('{$i}', _text(args[i]));
      }
      for (final MapEntry(:key, :value)
          in self?.values.entries ?? <MapEntry<String, MontyValue>>[]) {
        s = s.replaceAll('{$key}', _text(value));
      }

      return MontyString(s);
    }
    if (beh['raise'] case final Map<Object?, Object?> r) {
      throw ExternalException(
        (r['type'] as String?) ?? 'Exception',
        (r['message'] as String?) ?? '',
      );
    }
    if (beh['new_minus'] case final String field when self != null) {
      final values = {...self.values};
      final MontyInt(:value) = values[field]! as MontyInt;
      values[field] = MontyInt(value - (args.single as MontyInt).value);

      return _new(self.cls, values);
    }
    if (beh['returns_obj'] case final String label) return _objects[label]!;
    throw StateError('bad behaviour $beh');
  }

  /// Python's `str` of a value a format behaviour fills in.
  String _text(MontyValue v) => switch (v) {
    MontyString(:final value) => value,
    MontyInt(:final value) => '$value',
    _ => '$v',
  };

  /// A wrapper's `convert_value`, which always logs as the oracle's does.
  Object Function(String, Object) _hook(Map<String, Object?> ws) {
    final hook = ws['convert'] as String?;

    return (name, value) {
      log.add({'op': 'convert', 'name': name});
      if (hook == null) return value;
      if (hook == 'upper') {
        return value is MontyString
            ? MontyString(value.value.toUpperCase())
            : value;
      }
      if (hook.startsWith('redact:')) {
        return name == hook.split(':')[1] ? const MontyString('***') : value;
      }
      if (hook == 'wrap') {
        return value is HostObj ? _instanceWrapper(value, ws) : value;
      }
      throw StateError('unknown hook $hook');
    };
  }

  ({Object? eagerAttrs, Object? lazyAttrs, Object? allowedMethods}) _policy(
    Map<String, Object?> ws, [
    String prefix = '',
  ]) {
    Object? of(String key) => switch (ws['$prefix$key']) {
      final List<Object?> names => names.cast<String>().toList(),
      final other => other,
    };

    return (
      eagerAttrs: of('eager'),
      lazyAttrs: of('lazy'),
      allowedMethods: of('methods'),
    );
  }

  ClassInstance<HostObj> _instanceWrapper(HostObj o, Map<String, Object?> ws) {
    final cls = _classes[o.cls]!;
    final classWrapper = ws['class_wrapper'] as String?;
    final instance = ClassInstance(
      o,
      classType: classWrapper == null
          ? ClassType(cls)
          : _wrapper(classWrapper) as ClassType<HostObj>,
      policies: _policy(ws),
      convertValue: _hook(ws),
    );
    labelHostObject(instance.id, o.label);

    return instance;
  }

  MontyValue _wrapper(String name) => _wrappers[name] ??= _buildWrapper(name);

  MontyValue _buildWrapper(String name) {
    final ws = (wspecs[name]! as Map).cast<String, Object?>();
    if (ws['kind'] == 'instance') {
      return _instanceWrapper(_objects[ws['obj']! as String]!, ws);
    }

    return ClassType(
      _classes[ws['class']! as String]!,
      policies: _policy(ws),
      init: ws['init'] == true,
      instancePolicies: _policy(ws, 'instance_'),
      convertValue: _hook(ws),
    );
  }
}
