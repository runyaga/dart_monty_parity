import 'package:monty_parity/src/telemetry_recorder.dart';
import 'package:test/test.dart';

BigInt _now() =>
    BigInt.from(DateTime.now().microsecondsSinceEpoch) * BigInt.from(1000);

ParityTelemetry _dump(int spanSize, int measurementSize) {
  final recorder = ParityTelemetry()..reset();
  final span = recorder.startSpan('dump', 'app-request', {
    'state_bytes': spanSize,
  }, startTimeUnixNanos: _now());
  recorder
    ..endSpan(span, {}, endTimeUnixNanos: _now())
    ..recordMetric(
      'monty.snapshot.bytes',
      'histogram',
      'By',
      'Size of a session dump.',
      measurementSize,
      {'op': 'dump'},
      context: null,
    );

  return recorder;
}

void main() {
  test('snapshot sizes are checked locally before masking', () {
    expect(_dump(100, 100).result([100])['sizes_valid'], isTrue);
    expect(() => _dump(99, 100).result([100]), throwsStateError);
    expect(() => _dump(100, 99).result([100]), throwsStateError);
  });
  test('load state_bytes keeps its string type and checks its own length', () {
    final recorder = _dump(100, 100);
    final load = recorder.startSpan('load', 'app-request', {
      'state_bytes': '100',
    }, startTimeUnixNanos: _now());
    recorder.endSpan(load, {}, endTimeUnixNanos: _now());
    expect(recorder.result([100, 100])['sizes_valid'], isTrue);
    expect(() => recorder.result([100, 99]), throwsStateError);
  });
  test('timestamps must be epoch nanoseconds captured inside the run', () {
    final recorder = ParityTelemetry()..reset();
    final span = recorder.startSpan(
      'run code',
      'app-request',
      {},
      startTimeUnixNanos: BigInt.from(DateTime.now().millisecondsSinceEpoch),
    );
    recorder.endSpan(span, {}, endTimeUnixNanos: _now());
    expect(() => recorder.result([]), throwsStateError);
  });
  test('missing end and reversed timestamps fail', () {
    final recorder = ParityTelemetry()..reset();
    final earlier = _now();
    final span = recorder.startSpan(
      'run code',
      'app-request',
      {},
      startTimeUnixNanos: earlier + BigInt.one,
    );
    expect(() => recorder.result([]), throwsStateError);
    recorder.endSpan(span, {}, endTimeUnixNanos: earlier);
    expect(() => recorder.result([]), throwsStateError);
  });
  test('logs must occur within their parent span', () {
    final recorder = ParityTelemetry()..reset();
    final span = recorder.startSpan(
      'run code',
      'app-request',
      {},
      startTimeUnixNanos: _now(),
    );
    recorder
      ..emitLog(
        span,
        'INFO',
        'print stdout',
        {},
        timestampUnixNanos: BigInt.zero,
        severityNumber: 9,
      )
      ..endSpan(span, {}, endTimeUnixNanos: _now());
    expect(() => recorder.result([]), throwsStateError);
  });
  test('a metric must explicitly clear the ambient host context', () {
    final recorder = ParityTelemetry()..reset();
    recorder.runInContext('app-request', () {
      recorder.recordMetric(
        'monty.print.bytes',
        'counter',
        'By',
        '',
        7,
        {},
        context: recorder.currentContext(),
      );
    });
    expect(() => recorder.result([]), throwsStateError);
  });
}
