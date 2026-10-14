// Runs the parity cases baked into cases.g.dart through core's pool session
// in the browser (dart2js or dart2wasm): one pool for the run, its one Web
// Worker running core's committed
// lib/assets/dart_monty_core_proto_worker.{js,wasm}, Reset and reused from
// case to case. The pool uses core's own channel factory with no asset
// location, so the factory finds the assets itself; each channel it opens is
// recorded, so the leak checks see the Workers, not the pool's slot count.
// Prints each outcome on a marker line for tool/parity/run.py to collect.
//
// Then runs the probes baked into cases.g.dart (`parityProbes`,
// lib/src/session_probes.dart), each opening any other pool it configures
// through the same recording factory, and prints each on a probe marker line.
//
// Fails if any case or probe records a runner error, if one leaves more than
// the pool's one Worker alive, or if any Worker outlives the closed pools.
// Printing is the transport: tool/parity/run.py reads the marker lines.
// ignore_for_file: avoid_print
@TestOn('browser')
library;

import 'dart:convert';

import 'package:dart_monty_core/src/worker/channel_factory.dart';
import 'package:dart_monty_core/src/worker/monty_channel.dart';
import 'package:dart_monty_core/src/worker/monty_pool.dart';
import 'package:dart_monty_core/src/worker/spawn_handle.dart';
import 'package:dart_monty_core/src/worker/worker_pool.dart';
import 'package:monty_parity/monty_parity.dart';
import 'package:monty_parity/src/session_probes.dart';
import 'package:monty_parity/src/session_runner.dart';
import 'package:test/test.dart';

import 'cases.g.dart';

void main() {
  test('parity cases through the session (web)', () async {
    const backend = identical(1, 1.0) ? 'dart2js' : 'dart2wasm';
    final opened = <MontyChannel>[];
    int alive() => opened.where((ch) => ch.isAlive).length;
    Future<MontyPool> start(PoolOptions options) => MontyPool.startWith(
      options,
      open: () => _Recording(openChannel(), opened),
    );
    final pool = await start(const PoolOptions(maxProcesses: 1));
    final failed = <String>[];
    try {
      for (final text in parityCases) {
        final c = (jsonDecode(text) as Map).cast<String, Object?>();
        final id = c['id']! as String;
        Map<String, Object?> res;
        try {
          res = await runSessionCase(c, backend, pool);
        } on Object catch (e, st) {
          res = newOutcome(id, 'session', backend)
            ..['runner_error'] = '$e\n$st';
        }
        // The pool keeps at most its one Worker between cases.
        final live = alive();
        if (live > 1) res['runner_error'] ??= '$live Workers left running';
        if (res['runner_error'] != null) failed.add(id);
        print('$outcomeMarker${jsonEncode(res)}');
      }
      for (final id in parityProbes) {
        Map<String, Object?> res;
        try {
          res = await runSessionProbe(
            id,
            backend,
            pool,
            start,
            () => opened.length,
          );
        } on Object catch (e, st) {
          res = {
            'probe': id,
            'engine': 'session',
            'backend': backend,
            'events': const <Object?>[],
            'runner_error': '$e\n$st',
          };
        }
        // A probe's own pools are closed: only the run's one Worker remains.
        final live = alive();
        if (live > 1) res['runner_error'] ??= '$live Workers left running';
        if (res['runner_error'] != null) failed.add(id);
        print('$probeMarker${jsonEncode(res)}');
      }
    } finally {
      await pool.close();
      final left = alive();
      for (final ch in opened) {
        ch.kill();
      }
      expect(left, 0, reason: 'Workers alive after the pool closed');
    }
    expect(failed, isEmpty, reason: 'cases or probes with a runner error');
  }, timeout: const Timeout(Duration(minutes: 10)));
}

/// Core's own start, recording the channel it becomes ready with; a cancel
/// (core's startup timeout) reaches the start itself.
final class _Recording implements SpawnHandle {
  _Recording(this._inner, this._opened);

  final SpawnHandle _inner;
  final List<MontyChannel> _opened;
  Future<MontyChannel>? _ready;

  @override
  Future<MontyChannel> get ready => _ready ??= _record();

  Future<MontyChannel> _record() async {
    final channel = await _inner.ready;
    _opened.add(channel);

    return channel;
  }

  @override
  void cancel() => _inner.cancel();
}
