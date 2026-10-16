// Requires an F-2 checkout with its VM worker built, as tool/parity/run.py does.
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
  test(
    'budget probe checkout UnsupportedError remains a runner error ',
    () async {
      final pool = await startMonty(
        const PoolOptions(minProcesses: 0, maxProcesses: 1),
        open: () => throw UnsupportedError('checkout failure'),
      );
      addTearDown(pool.close);
      for (final backend in ['vm', 'dart2js']) {
        final result = await runSessionProbe(
          'api_memory_budget',
          backend,
          pool,
          startMonty,
          () => 0,
        );
        expect(result['refused'], isNull);
        expect(result['runner_error'], contains('checkout failure'));
      }
    },
  );

  for (final options in [
    {'record_dispatch': true, 'dispatch': 'manual'},
    {'sleep_access': true},
  ]) {
    test('dispatch options apply without feed_start: $options', () async {
      final pool = await startMonty(const PoolOptions(maxProcesses: 1));
      addTearDown(pool.close);
      final result = await runSessionCase(
        {
          'id': 'dispatch_options',
          'code': 'import time\ntime.sleep(0)\n7',
          ...options,
          'os': {
            'system.sleep': [
              {
                'return': {'t': 'none'},
              },
            ],
          },
        },
        'vm',
        pool,
      );
      final step = (result['steps']! as List).single as Map;
      expect(step['exception'], isNull);
      expect(step['value'], {'t': 'int', 'v': '7'});
      if (options['dispatch'] == 'manual') {
        expect(
          (step['host_calls']! as List).first,
          containsPair('ev', 'start'),
        );
        expect(step['os_calls'], hasLength(1));
      } else {
        expect(step['host_calls'], [
          {'ev': 'handler', 'name': 'system.sleep'},
        ]);
      }
    });
  }
  test('refmem ignores per-feed mount selection', () async {
    final pool = await startMonty(const PoolOptions(maxProcesses: 1));
    addTearDown(pool.close);
    final result = await runSessionCase(
      {
        'id': 'refmem_selection',
        'refmem': true,
        'feeds': [
          {
            'code': "from pathlib import Path\nPath('/data/a').read_text()",
            'mount_indices': [0],
          },
        ],
        'fs': {
          'mounts': [
            {'virtual': '/data'},
          ],
          'files': {
            '/data/a': {'text': 'A'},
          },
        },
      },
      'vm',
      pool,
      engine: 'refmem',
    );
    final step = (result['steps']! as List).single as Map;
    expect(step['exception'], isNull);
    expect(step['value'], {'t': 'str', 'v': 'A'});
  });
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
    'mount budget probes measure host mount limits on the VM',
    _budgetProbes,
  );
  test('a missed restore suspension is a runner failure', () async {
    final pool = await startMonty(const PoolOptions(maxProcesses: 1));
    addTearDown(pool.close);
    await expectLater(
      runSessionCase(
        {
          'id': 'missing_restore',
          'code': '1',
          'local_refusal': true,
          'restore_snapshot_at': 'system.sleep',
        },
        'vm',
        pool,
      ),
      throwsA(isA<StateError>()),
    );
  });
  for (final delay in ['0', '0.001']) {
    test('async dispatch requires an OS snapshot (delay=$delay)', () async {
      final pool = await startMonty(const PoolOptions(maxProcesses: 1));
      addTearDown(pool.close);
      final pending = runSessionCase(
        {
          'id': 'async_dispatch_snapshot',
          'code': 'import asyncio\nawait asyncio.sleep($delay)\n7',
          'record_dispatch': true,
          'expect_os_snapshot': 'system.async_sleep',
          'sleep_access': true,
        },
        'vm',
        pool,
        engine: 'core',
      );
      if (delay == '0') {
        await expectLater(pending, throwsA(isA<StateError>()));
        return;
      }
      final result = await pending;
      final steps = (result['steps']! as List).cast<Map<String, Object?>>();
      final step = steps.single;
      expect(step['exception'], isNull);
      expect(step['value'], {'t': 'int', 'v': '7'});
      expect(step['host_calls'], [
        {
          'ev': 'start',
          'name': 'system.async_sleep',
          'is_os': true,
          'handler_calls': 0,
        },
        {'ev': 'handler', 'name': 'system.async_sleep'},
      ]);
    });
  }
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
    expect(result['events'], isNotEmpty);
    expect(result['unsupported'], isNull);
    expect(result['refused'], isNull);
  }
}
