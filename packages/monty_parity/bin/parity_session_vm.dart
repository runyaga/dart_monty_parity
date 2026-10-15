// Runs parity cases through core's public `Monty` / `MontySession` on the
// Dart VM: one pool for the run (one worker process, the executable that
// core's tool/build_worker.sh writes into the core checkout), Reset and
// reused from case to case. The `core` and `session` engines both run here;
// they differ only in the rules the scorer applies (session_runner.dart).
//
// usage: dart run bin/parity_session_vm.dart OUTDIR [case.json ...]
//            [--probes=ID,ID,...] [--engine=core|session]
//
// Writes OUTDIR/<case_id>/<engine>-vm.json, then, for each probe,
// OUTDIR/<probe>/<engine>-vm.json (lib/src/session_probes.dart; a probe opens
// any other pool it configures). A case or probe that throws in the runner
// records `runner_error`. Exits 1 if any case or probe records a runner
// error, if one leaves more than the pool's one worker running, or if any
// worker outlives the closed pools.
import 'dart:convert';
import 'dart:io';

import 'package:dart_monty_core/src/worker/channel_factory.dart';
import 'package:dart_monty_core/src/worker/monty.dart';
import 'package:dart_monty_core/src/worker/process_channel.dart';
import 'package:dart_monty_core/src/worker/worker_pool.dart';
import 'package:monty_parity/monty_parity.dart';
import 'package:monty_parity/src/session_probes.dart';
import 'package:monty_parity/src/session_runner.dart';

const enc = JsonEncoder.withIndent(' ');

Future<void> main(List<String> args) async {
  final outdir = args.first;
  const flag = '--probes=';
  const engineFlag = '--engine=';
  final probes = [
    for (final a in args.skip(1))
      if (a.startsWith(flag))
        ...a.substring(flag.length).split(',').where((p) => p.isNotEmpty),
  ];
  final engine = [
    for (final a in args.skip(1))
      if (a.startsWith(engineFlag)) a.substring(engineFlag.length),
    'session',
  ].first;
  if (!sessionEngines.contains(engine)) {
    stderr.writeln('unknown engine $engine; expected one of $sessionEngines');
    exit(2);
  }
  var failed = false;
  var spawned = 0;
  final pool = await startMonty(const PoolOptions(maxProcesses: 1));
  for (final path in args.skip(1).where((a) => !a.startsWith('--'))) {
    final c = (jsonDecode(File(path).readAsStringSync()) as Map)
        .cast<String, Object?>();
    final id = c['id']! as String;
    final d = Directory('$outdir/$id')..createSync(recursive: true);
    Map<String, Object?> res;
    try {
      res = await runSessionCase(c, 'vm', pool, engine: engine);
    } on Object catch (e, st) {
      res = newOutcome(id, engine, 'vm')..['runner_error'] = '$e\n$st';
    }
    // The pool keeps at most its one worker; a killed one must be gone.
    final live = await _settled(1);
    if (live.isNotEmpty) {
      res['runner_error'] ??= 'worker processes left running: $live';
    }
    if (res['runner_error'] != null) failed = true;
    File(
      '${d.path}/$engine-vm.json',
    ).writeAsStringSync('${enc.convert(res)}\n');
    stdout.writeln(
      '$engine-vm $id: ${res['runner_error'] == null ? 'ok' : 'ERROR'}',
    );
  }
  for (final id in probes) {
    Map<String, Object?> res;
    try {
      res = await runSessionProbe(
        id,
        'vm',
        pool,
        (options) => startMonty(
          options,
          open: () {
            spawned++;

            return openChannel(workerLocation: options.workerLocation);
          },
        ),
        () => spawned,
        engine: engine,
      );
    } on Object catch (e, st) {
      res = {
        'probe': id,
        'engine': engine,
        'backend': 'vm',
        'events': const <Object?>[],
        'runner_error': '$e\n$st',
      };
    }
    // A probe's own pools are closed: only the run's one worker may remain.
    final live = await _settled(1);
    if (live.isNotEmpty) {
      res['runner_error'] ??= 'worker processes left running: $live';
    }
    if (res['runner_error'] != null) failed = true;
    final d = Directory('$outdir/$id')..createSync(recursive: true);
    File(
      '${d.path}/$engine-vm.json',
    ).writeAsStringSync('${enc.convert(res)}\n');
    stdout.writeln(
      '$engine-vm probe $id: ${res['runner_error'] == null ? 'ok' : 'ERROR'}',
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
