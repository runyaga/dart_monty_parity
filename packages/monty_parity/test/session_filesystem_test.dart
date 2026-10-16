// Requires an F-1 checkout with its VM worker built, as tool/parity/run.py does.
@TestOn('vm')
library;

import 'package:dart_monty_core/src/worker/monty.dart';
import 'package:dart_monty_core/src/worker/process_channel.dart';
import 'package:dart_monty_core/src/worker/worker_locator.dart';
import 'package:dart_monty_core/src/worker/worker_pool.dart';
import 'package:monty_parity/src/session_probes.dart';
import 'package:monty_parity/src/session_runner.dart';
import 'package:monty_parity/src/worker_runner.dart';
import 'package:test/test.dart';

void main() {
  test('cwd reaches the worker feed', () async {
    final channel = await ProcessChannel.spawn(await locateWorker());
    addTearDown(channel.close);
    final result = await runWorkerCase(
      {
        'id': 'worker_cwd',
        'feeds': ['import os\nos.getcwd()', 'os.getcwd()'],
        'cwd': '/work',
      },
      'vm',
      () async => channel,
    );
    final steps = (result['steps']! as List).cast<Map<String, Object?>>();
    expect(steps, hasLength(2));
    for (final step in steps) {
      expect(step['value'], {'t': 'str', 'v': '/work'});
    }
    expect(result['unsupported'], isEmpty);
  });
  for (final byHand in [false, true]) {
    test('cwd reaches public feed (manual=$byHand)', () async {
      final pool = await startMonty(const PoolOptions(maxProcesses: 1));
      addTearDown(pool.close);
      final result = await runSessionCase(
        {
          'id': 'cwd',
          'code': 'import os\nos.getcwd()',
          'cwd': '/work',
          'local_refusal': byHand,
        },
        'vm',
        pool,
      );
      final steps = (result['steps']! as List).cast<Map<String, Object?>>();
      expect(steps, hasLength(1));
      expect(steps[0]['value'], {'t': 'str', 'v': '/work'});
      expect(result['unsupported'], isEmpty);
    });
  }
  test(
    'callback probe records String payload, path, order and fresh reads',
    _callbackProbe,
  );
  test(
    'mount budget probes report unsupported without running a feed',
    _budgetProbes,
  );
}

Future<void> _callbackProbe() async {
  final pool = await startMonty(const PoolOptions(maxProcesses: 1));
  addTearDown(pool.close);
  final result = await runSessionProbe(
    'api_callback_files',
    'vm',
    pool,
    startMonty,
    () => 0,
  );
  expect(result['runner_error'], isNull);
  final events = (result['events']! as List).cast<Map<String, Object?>>();
  final host = events.where((event) => event['ev'] == 'host').toList();
  expect(host, [
    {'ev': 'host', 'op': 'read', 'path': '/cb/f.txt'},
    {
      'ev': 'host',
      'op': 'write',
      'path': '/cb/f.txt',
      'type': 'String',
      'text': 'new',
    },
    {'ev': 'host', 'op': 'read', 'path': '/cb/f.txt'},
  ]);
  expect(
    events,
    contains(
      containsPair('value', {
        't': 'tuple',
        'items': [
          {'t': 'str', 'v': 'initial'},
          {'t': 'str', 'v': 'callback-saw:new'},
          {'t': 'bool', 'v': true},
        ],
      }),
    ),
  );
}

Future<void> _budgetProbes() async {
  final pool = await startMonty(const PoolOptions(maxProcesses: 1));
  addTearDown(pool.close);
  for (final id in [
    'api_write_budget_feed1',
    'api_write_budget_feed2',
    'api_memory_budget',
  ]) {
    final result = await runSessionProbe(id, 'vm', pool, startMonty, () => 0);
    expect(result['runner_error'], isNull);
    expect(result['events'], isEmpty);
    expect(result['unsupported'], contains('fs.mounts'));
  }
}
