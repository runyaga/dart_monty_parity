// Crashes through core's worker driver on the Dart VM: each one surfaces as a
// typed MontyCrashedError, the parent survives, and a fresh worker then
// answers `1 + 1`. Run by tool/parity/run.py vm --engine worker.
//
// Every session sets max_memory: an unbounded worker that runs away can take
// gigabytes before anything stops it.
@TestOn('vm')
library;

import 'dart:async';

import 'package:dart_monty_core/dart_monty_core.dart';
import 'package:dart_monty_core/src/worker/monty_channel.dart';
import 'package:dart_monty_core/src/worker/process_channel.dart';
import 'package:dart_monty_core/src/worker/proto_session.dart';
import 'package:dart_monty_core/src/worker/session_settings.dart';
import 'package:dart_monty_core/src/worker/worker_locator.dart';
import 'package:test/test.dart';

const int _maxMemory = 64 * 1024 * 1024;

/// Equality of two 300,000-deep nested lists with the recursion limit lifted,
/// so the interpreter's native recursion overflows the worker's stack before
/// any Python limit stops it.
const _deepCompare =
    'x = []\nfor i in range(300000):\n    x = [x]\n'
    'y = []\nfor i in range(300000):\n    y = [y]\nx == y';

/// A session on a new worker process, closed after the test.
Future<(ProtoSession, ProcessChannel)> _open(SessionConfig config) async {
  final channel = await ProcessChannel.spawn(await locateWorker());
  addTearDown(channel.close);

  return (await ProtoSession.open(channel, config), channel);
}

/// A fresh worker answers `1 + 1`.
Future<void> _freshWorkerAnswers() async {
  final (session, _) = await _open(
    const SessionConfig(limits: ResourceLimits(maxMemory: _maxMemory)),
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
  tearDown(() async {
    final deadline = DateTime.now().add(const Duration(seconds: 5));
    while (ProcessChannel.live.isNotEmpty &&
        DateTime.now().isBefore(deadline)) {
      await Future<void>.delayed(const Duration(milliseconds: 20));
    }
    final leaked = ProcessChannel.live.toList();
    for (final channel in leaked) {
      channel.kill();
    }
    expect(
      [for (final c in leaked) c.pid],
      isEmpty,
      reason: 'worker processes left running after the test',
    );
  });

  test('deadline kill is MontyCrashedError(killed); a fresh worker '
      'answers', () async {
    final (session, channel) = await _open(
      const SessionConfig(limits: ResourceLimits(maxMemory: _maxMemory)),
    );
    final spinning = session.feed('while True:\n    pass');
    Timer(const Duration(milliseconds: 300), channel.kill);
    final crash = await _crashOf(spinning);
    expect(crash.cause, CrashCause.killed);
    expect(channel.isAlive, isFalse);
    await _freshWorkerAnswers();
  });

  test('native stack overflow is MontyCrashedError(vanished) with the '
      'abort on stderr; a fresh worker answers', () async {
    final (session, channel) = await _open(
      const SessionConfig(
        limits: ResourceLimits(
          maxMemory: 512 * 1024 * 1024,
          maxRecursionDepth: 100000000,
        ),
      ),
    );
    final crash = await _crashOf(session.feed(_deepCompare));
    expect(crash.cause, CrashCause.vanished);
    expect(crash.exitCode, -6, reason: 'SIGABRT: $crash');
    expect(crash.stderrTail, contains('has overflowed its stack'));
    expect(channel.isAlive, isFalse);
    await _freshWorkerAnswers();
  });
}
