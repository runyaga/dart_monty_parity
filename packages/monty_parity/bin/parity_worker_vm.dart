// Runs parity cases through core's worker on the Dart VM: each case on a
// fresh worker process (ProcessChannel), the executable that core's
// tool/build_worker.sh writes into the core checkout.
//
// usage: dart run bin/parity_worker_vm.dart OUTDIR case.json [case.json ...]
//
// Writes OUTDIR/<case_id>/worker-vm.json. A case that throws in the runner
// records `runner_error`. Exits 1 if any worker process outlives its case.
import 'dart:convert';
import 'dart:io';

import 'package:dart_monty_core/src/worker/monty_channel.dart';
import 'package:dart_monty_core/src/worker/process_channel.dart';
import 'package:dart_monty_core/src/worker/worker_locator.dart';
import 'package:monty_parity/monty_parity.dart';
import 'package:monty_parity/src/worker_runner.dart';

const enc = JsonEncoder.withIndent(' ');

Future<MontyChannel> _spawn() async =>
    ProcessChannel.spawn(await locateWorker());

Future<void> main(List<String> args) async {
  final outdir = args.first;
  var leaked = false;
  for (final path in args.skip(1)) {
    final c = (jsonDecode(File(path).readAsStringSync()) as Map)
        .cast<String, Object?>();
    final id = c['id']! as String;
    final d = Directory('$outdir/$id')..createSync(recursive: true);
    Map<String, Object?> res;
    try {
      res = await runWorkerCase(c, 'vm', _spawn);
    } on Object catch (e, st) {
      res = newOutcome(id, 'worker', 'vm')..['runner_error'] = '$e\n$st';
    }
    final live = await _drained();
    if (live.isNotEmpty) {
      leaked = true;
      res['runner_error'] ??= 'worker processes left running: $live';
    }
    File('${d.path}/worker-vm.json').writeAsStringSync('${enc.convert(res)}\n');
    stdout.writeln(
      'worker-vm $id: ${res['runner_error'] == null ? 'ok' : 'ERROR'}',
    );
  }
  exit(leaked ? 1 : 0);
}

/// The pids of workers still running a short grace after a case closed its
/// session; each is killed so it cannot outlive the run.
Future<List<int>> _drained() async {
  final deadline = DateTime.now().add(const Duration(seconds: 5));
  while (ProcessChannel.live.isNotEmpty && DateTime.now().isBefore(deadline)) {
    await Future<void>.delayed(const Duration(milliseconds: 20));
  }
  final live = ProcessChannel.live.toList();
  for (final channel in live) {
    channel.kill();
  }

  return [for (final channel in live) channel.pid];
}
