import 'dart:async';
import 'dart:convert';

import 'package:dart_monty_core/dart_monty_core.dart';

/// The process-wide recorder; installed once by each runner entry point.
final parityTelemetry = ParityTelemetry();

/// Installs recording before any case or probe creates a pool.
void installParityTelemetry() => instrumentTelemetry(parityTelemetry);

BigInt _now() =>
    BigInt.from(DateTime.now().microsecondsSinceEpoch) * BigInt.from(1000);

/// A raw span handle, retaining timestamp precision on every backend.
final class _Span {
  _Span(this.name, this.parent, this.attributes, this.start);

  final String name;
  final Object? parent;
  final Map<String, Object> attributes;
  final BigInt start;
  BigInt? end;
}

/// An SDK boundary recorder. Validation happens after the scenario, so core
/// cannot swallow an assertion as an adapter failure and disable a signal.
final class ParityTelemetry implements MontyTelemetry {
  final _spans = <_Span>[];
  final _logs =
      <
        ({
          Object? span,
          String severity,
          int number,
          String body,
          Map<String, Object> attributes,
          BigInt time,
        })
      >[];
  final _metrics = <Map<String, Object?>>[];
  BigInt _begin = BigInt.zero;

  /// Clears buffers only; instrumentation remains installed.
  void reset() {
    _spans.clear();
    _logs.clear();
    _metrics.clear();
    _begin = _now();
  }

  @override
  Object? currentContext() => Zone.current[#parityTelemetry];

  @override
  Object? contextWithSpan(Object? context, Object span) => span;

  @override
  R runInContext<R>(Object? context, R Function() body) =>
      runZoned(body, zoneValues: {#parityTelemetry: context});

  /// The current span's name, observed inside callbacks and snapshot contexts.
  String? contextName(Object? context) => switch (context) {
    _Span(:final name) => name,
    String() => context,
    _ => null,
  };

  @override
  Object startSpan(
    String name,
    Object? parent,
    Map<String, Object> attributes, {
    required BigInt startTimeUnixNanos,
  }) {
    final span = _Span(name, parent, Map.of(attributes), startTimeUnixNanos);
    _spans.add(span);

    return span;
  }

  @override
  void endSpan(
    Object span,
    Map<String, Object> attributes, {
    required BigInt endTimeUnixNanos,
  }) {
    final record = span as _Span;
    record.attributes.addAll(attributes);
    record.end = endTimeUnixNanos;
  }

  @override
  void emitLog(
    Object? span,
    String severity,
    String body,
    Map<String, Object> attributes, {
    required BigInt timestampUnixNanos,
    required int severityNumber,
  }) {
    _logs.add((
      span: span,
      severity: severity,
      number: severityNumber,
      body: body,
      attributes: Map.of(attributes),
      time: timestampUnixNanos,
    ));
  }

  @override
  // Mirrors the public MontyTelemetry signature.
  // ignore: number-of-parameters
  void recordMetric(
    String name,
    String kind,
    String unit,
    String description,
    num value,
    Map<String, Object> attributes, {
    required Object? context,
  }) {
    _metrics.add({
      'name': name,
      'kind': kind,
      'unit': unit,
      'description': description,
      'value': value,
      'attributes': Map.of(attributes),
      'empty_context': context == null,
    });
  }

  /// Validates local timestamps/sizes, then masks only the contract's fields.
  Map<String, Object?> result(List<int> sizes) {
    final end = _now();
    final spans = _spans.map((s) => _spanJson(s, end)).toList();
    final stateSizes = [
      for (final s in _spans)
        if (s.attributes['state_bytes'] case final Object size)
          int.parse('$size'),
    ];
    _require(
      jsonEncode(stateSizes) == jsonEncode(sizes),
      'state_bytes $stateSizes != $sizes',
    );
    final logs = _logJson(end);
    final snapshots = [
      for (final m in _metrics)
        if (m['name'] == 'monty.snapshot.bytes') m['value'],
    ];
    _require(
      jsonEncode(snapshots) == jsonEncode(sizes.take(1).toList()),
      'snapshot.bytes $snapshots != $sizes',
    );
    final metrics = _metrics.map(_metricJson).toList()
      ..sort((a, b) => _canonical(a).compareTo(_canonical(b)));

    return {
      'ev': 'telemetry',
      'spans': spans,
      'logs': logs,
      'metrics': metrics,
      'sizes_valid': true,
    };
  }

  Map<String, Object?> _spanJson(_Span span, BigInt end) {
    final finish = span.end;
    _require(finish != null, 'unclosed span ${span.name}');
    _timestamp(span.start, end);
    _timestamp(finish!, end);
    _require(span.start <= finish, 'span timestamp order');
    if (span.parent case final _Span parent) {
      _require(
        parent.start <= span.start &&
            parent.end != null &&
            finish <= parent.end!,
        'span outside parent',
      );
    }
    if (span.name.startsWith('session ')) {
      _require(span.attributes['monty_version'] == '1.1.0', 'monty_version');
    }

    return {
      'name': span.name,
      'parent': _parent(span.parent),
      'attributes': _attributes(span.attributes),
      'timestamps_valid': true,
    };
  }

  List<Map<String, Object?>> _logJson(BigInt end) {
    var last = _begin;
    final logs = <Map<String, Object?>>[];
    for (final log in _logs) {
      _timestamp(log.time, end);
      _require(last <= log.time, 'log timestamp order');
      last = log.time;
      final parent = log.span! as _Span;
      _require(
        parent.start <= log.time &&
            parent.end != null &&
            log.time <= parent.end!,
        'log outside parent',
      );
      logs.add({
        'parent': _parent(parent),
        'severity': log.severity,
        'severity_number': log.number,
        'body': log.body,
        'attributes': _attributes(log.attributes),
        'timestamp_valid': true,
      });
    }

    return logs;
  }

  Object? _parent(Object? context) =>
      context is _Span ? _spans.indexOf(context) : context;

  void _timestamp(BigInt time, BigInt end) => _require(
    _begin <= time && time <= end,
    'timestamp must be epoch nanoseconds within run',
  );
}

Map<String, Object?> _metricJson(Map<String, Object?> raw) {
  final m = Map<String, Object?>.of(raw);
  final value = m['value']! as num;
  _require(m['empty_context'] == true, 'metric inherited ambient context');
  _require(value >= 0 || m['kind'] == 'up_down_counter', 'negative metric');
  if (m['unit'] == 's') {
    m['value'] = '<seconds>';
  } else if (m['name'] == 'monty.snapshot.bytes' ||
      m['name'] == 'monty.wire.frame.bytes') {
    _require(value is int && value > 0, 'byte measurement');
    m['value'] = '<bytes>';
  }

  return m;
}

const _omit = {
  'code.file.path',
  'code.line.number',
  'code.module.name',
  'busy_ns',
  'idle_ns',
  'worker_pid',
};
const _mask = {
  'total_execution_micros',
  'feed_execution_micros',
  'call_id',
  'state_bytes',
};

Map<String, Object> _attributes(Map<String, Object> raw) => {
  for (final MapEntry(:key, :value) in raw.entries)
    if (!_omit.contains(key) &&
        !key.startsWith('logfire.') &&
        !key.startsWith('thread.'))
      key: _mask.contains(key) ? {'type': _type(value), 'masked': true} : value,
};

String _type(Object value) => switch (value) {
  String() => 'str',
  bool() => 'bool',
  int() => 'int',
  double() => 'float',
  _ => throw StateError('unexpected attribute type ${value.runtimeType}'),
};

String _canonical(Map<String, Object?> value) {
  final keys = value.keys.toList()..sort();

  return jsonEncode({
    for (final key in keys)
      key: value[key] is Map<String, Object>
          ? jsonDecode(_canonical(value[key]! as Map<String, Object>))
          : value[key],
  });
}

void _require(bool valid, String message) {
  if (!valid) throw StateError(message);
}
