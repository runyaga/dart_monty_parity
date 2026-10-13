// Crashes through core's worker driver in the browser (dart2js or
// dart2wasm): each one surfaces as a typed MontyCrashedError, the page
// survives, and a fresh Worker then answers `1 + 1`. Run by
// tool/parity/run.py {dart2js,dart2wasm} --engine worker.
//
// Every session sets max_memory.
@TestOn('browser')
library;

import 'dart:async';

import 'package:dart_monty_core/dart_monty_core.dart';
import 'package:dart_monty_core/src/worker/monty_channel.dart';
import 'package:dart_monty_core/src/worker/proto_session.dart';
import 'package:dart_monty_core/src/worker/worker_channel.dart';
import 'package:test/test.dart';

/// Core's lib/ as the test server exposes it.
const _assets = 'packages/dart_monty_core/assets';

const int _maxMemory = 64 * 1024 * 1024;

/// Equality of two 300,000-deep nested lists with the recursion limit lifted,
/// so the interpreter's native recursion overflows the wasm stack (a trap)
/// before any Python limit stops it.
const _deepCompare =
    'x = []\nfor i in range(300000):\n    x = [x]\n'
    'y = []\nfor i in range(300000):\n    y = [y]\nx == y';

final _cache = WasmModuleCache('$_assets/dart_monty_core_proto_worker.wasm');

/// Every channel a test opened; each test must leave all of them dead.
final _opened = <WorkerChannel>[];

/// A session on a new Worker.
Future<(ProtoSession, WorkerChannel)> _open(
  SessionConfig config, {
  bool forceNoJspi = false,
}) async {
  final channel = await WorkerChannel.spawn(
    _cache,
    workerUrl: '$_assets/dart_monty_core_proto_worker.js',
    forceNoJspi: forceNoJspi,
  );
  _opened.add(channel);

  return (await ProtoSession.open(channel, config), channel);
}

/// A fresh Worker answers `1 + 1`.
Future<void> _freshWorkerAnswers({bool forceNoJspi = false}) async {
  final (session, _) = await _open(
    const SessionConfig(maxMemoryBytes: _maxMemory),
    forceNoJspi: forceNoJspi,
  );
  final r = await session.feed('1 + 1');
  expect(r.value, isA<MontyInt>().having((v) => v.value, 'value', 2));
  await session.close();
}

/// The [MontyCrashedError] [future] fails with.
Future<MontyCrashedError> _crashOf(Future<Object?> future) async {
  try {
    await future;
    // The error under test: a dead worker is reported as an Error.
    // ignore: avoid_catching_errors
  } on MontyCrashedError catch (e) {
    return e;
  }

  return fail('completed without a MontyCrashedError');
}

void main() {
  const backend = identical(1, 1.0) ? 'dart2js' : 'dart2wasm';

  tearDown(() {
    final alive = _opened.where((c) => c.isAlive).length;
    for (final c in _opened) {
      c.kill();
    }
    _opened.clear();
    expect(alive, 0, reason: 'a test left $alive Worker(s) running');
  });

  test('deadline kill is MontyCrashedError(killed); a fresh Worker '
      'answers ($backend)', () async {
    final (session, channel) = await _open(
      const SessionConfig(maxMemoryBytes: _maxMemory),
    );
    final spinning = session.feed('while True:\n    pass');
    Timer(const Duration(milliseconds: 300), channel.kill);
    final crash = await _crashOf(spinning);
    expect(crash.cause, CrashCause.killed);
    expect(channel.isAlive, isFalse);
    await _freshWorkerAnswers();
  });

  for (final noJspi in [false, true]) {
    final mode = noJspi ? 'forced fallback' : 'JSPI';
    test('a wasm trap is MontyCrashedError(vanished); a fresh Worker '
        'answers ($mode, $backend)', () async {
      final (session, channel) = await _open(
        const SessionConfig(
          maxMemoryBytes: 512 * 1024 * 1024,
          maxRecursionDepth: 100000000,
        ),
        forceNoJspi: noJspi,
      );
      expect(channel.jspi, !noJspi);
      final crash = await _crashOf(session.feed(_deepCompare));
      expect(crash.cause, CrashCause.vanished);
      // Chrome's text for a wasm stack overflow.
      expect(crash.message, contains('RangeError'));
      expect(channel.isAlive, isFalse);
      await _freshWorkerAnswers(forceNoJspi: noJspi);
    });
  }
}
