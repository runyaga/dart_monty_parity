// Runs parity cases through core's pool session on the Dart VM: one pool
// for the run (one worker process, the executable that core's
// tool/build_worker.sh writes into the core checkout), Reset and reused from
// case to case.
//
// usage: dart run bin/parity_session_vm.dart OUTDIR case.json [case.json ...]
//
// Writes OUTDIR/<case_id>/session-vm.json. A case that throws in the runner
// records `runner_error`. Exits 1 if any case records a runner error, if a
// case leaves more than the pool's one worker running, or if any worker
// outlives the closed pool.
import 'dart:convert';
import 'dart:io';

import 'package:dart_monty_core/src/worker/monty_pool.dart';
import 'package:dart_monty_core/src/worker/process_channel.dart';
import 'package:monty_parity/monty_parity.dart';
import 'package:monty_parity/src/session_runner.dart';

const enc = JsonEncoder.withIndent(' ');

Future<void> main(List<String> args) async {
  final outdir = args.first;
  var failed = false;
  final pool = await MontyPool.start(maxProcesses: 1);
  for (final path in args.skip(1)) {
    final c = (jsonDecode(File(path).readAsStringSync()) as Map)
        .cast<String, Object?>();
    final id = c['id']! as String;
    final d = Directory('$outdir/$id')..createSync(recursive: true);
    Map<String, Object?> res;
    try {
      res = await runSessionCase(c, 'vm', pool);
    } on Object catch (e, st) {
      res = newOutcome(id, 'session', 'vm')..['runner_error'] = '$e\n$st';
    }
    // The pool keeps at most its one worker; a killed one must be gone.
    final live = await _settled(1);
    if (live.isNotEmpty) {
      res['runner_error'] ??= 'worker processes left running: $live';
    }
    if (res['runner_error'] != null) failed = true;
    File(
      '${d.path}/session-vm.json',
    ).writeAsStringSync('${enc.convert(res)}\n');
    stdout.writeln(
      'session-vm $id: ${res['runner_error'] == null ? 'ok' : 'ERROR'}',
    );
  }
  await pool.close();
  final left = await _settled(0);
  if (left.isNotEmpty) {
    failed = true;
    stdout.writeln('worker processes left after the pool closed: $left');
  }
  exit(failed ? 1 : 0);
}

/// The pids of workers beyond [allowed] still running a short grace after a
/// case; each is killed so it cannot outlive the run.
Future<List<int>> _settled(int allowed) async {
  final deadline = DateTime.now().add(const Duration(seconds: 5));
  while (ProcessChannel.live.length > allowed &&
      DateTime.now().isBefore(deadline)) {
    await Future<void>.delayed(const Duration(milliseconds: 20));
  }
  if (ProcessChannel.live.length <= allowed) return const [];
  final live = ProcessChannel.live.toList();
  for (final channel in live) {
    channel.kill();
  }

  return [for (final channel in live) channel.pid];
}
