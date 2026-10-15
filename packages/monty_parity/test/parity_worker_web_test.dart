// Runs the parity cases baked into cases.g.dart through core's worker in the
// browser (dart2js or dart2wasm): each case on a fresh Web Worker
// (WorkerChannel) running core's committed
// lib/assets/dart_monty_core_worker.{js,wasm}, all sharing one
// compiled module. Prints each outcome on a marker line for
// tool/parity/run.py to collect.
// Printing is the transport: tool/parity/run.py reads the marker lines.
// ignore_for_file: avoid_print
@TestOn('browser')
library;

import 'dart:convert';

import 'package:dart_monty_core/src/worker/worker_channel.dart';
import 'package:monty_parity/monty_parity.dart';
import 'package:monty_parity/src/worker_runner.dart';
import 'package:test/test.dart';

import 'cases.g.dart';

/// Core's lib/ as the test server exposes it.
const _assets = 'packages/dart_monty_core/assets';

void main() {
  test('parity cases through the worker (web)', () async {
    const backend = identical(1, 1.0) ? 'dart2js' : 'dart2wasm';
    final cache = WasmModuleCache(
      '$_assets/dart_monty_core_worker.wasm',
    );
    final opened = <WorkerChannel>[];
    Future<WorkerChannel> spawn() async {
      final channel = await WorkerChannel.spawn(
        cache,
        workerUrl: '$_assets/dart_monty_core_worker.js',
      );
      opened.add(channel);

      return channel;
    }

    final jspi = await _jspi(cache);
    for (final text in parityCases) {
      final c = (jsonDecode(text) as Map).cast<String, Object?>();
      final id = c['id']! as String;
      Map<String, Object?> res;
      try {
        res = await runWorkerCase(c, backend, spawn);
      } on Object catch (e, st) {
        res = newOutcome(id, 'worker', backend)..['runner_error'] = '$e\n$st';
      }
      final live = opened.where((ch) => ch.isAlive).length;
      if (live > 0) {
        res['runner_error'] ??= '$live Worker(s) left running';
      }
      for (final ch in opened) {
        ch.kill();
      }
      opened.clear();
      if (res['notes'] case final Map<String, Object?> notes) {
        notes['jspi'] = jspi;
      }
      print('$outcomeMarker${jsonEncode(res)}');
    }
  }, timeout: const Timeout(Duration(minutes: 10)));
}

/// Whether the Workers dispatch under JSPI on this browser.
Future<bool> _jspi(WasmModuleCache cache) async {
  final probe = await WorkerChannel.spawn(
    cache,
    workerUrl: '$_assets/dart_monty_core_worker.js',
  );
  final jspi = probe.jspi;
  await probe.close();

  return jspi;
}
