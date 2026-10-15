// The session engine's probes: API-level transcripts through dart_monty_core's
// internal pool session (`MontyPool`, `PoolSession`), the path that becomes
// core's public `Monty` / `MontySession` at the cutover. Each probe emits the
// events tool/parity/oracle/run_probes.py records for upstream, and
// tool/parity/probes_compare.py compares the two with no named exception:
// suspension positions included. Web-safe.
//
// A probe of the gate (tool/parity/c1-gate.json) that the session cannot
// express yet records `unimplemented` with the reason instead of events; the
// scorer counts it as NO-OUTCOME, which fails once its stage has landed.
//
// Implementation imports are the point of this engine: it measures core's
// internal session path before that path has a public API.
// ignore_for_file: implementation_imports
import 'dart:async';
import 'dart:typed_data';

import 'package:dart_monty_core/dart_monty_core.dart'
    hide MontyComplete, MontyError, MontySyntaxError, MontyTypingError;
import 'package:dart_monty_core/src/worker/monty_error.dart';
import 'package:dart_monty_core/src/worker/monty_pool.dart';
import 'package:dart_monty_core/src/worker/monty_snapshot.dart';
import 'package:dart_monty_core/src/worker/pool_session.dart';
import 'package:dart_monty_core/src/worker/proto_session.dart';
import 'package:dart_monty_core/src/worker/resource_limits.dart';
import 'package:dart_monty_core/src/worker/worker_pool.dart';
import 'package:monty_parity/src/worker_runner.dart';

/// Starts a pool with [options] for one probe; the probe closes it.
typedef StartPool = Future<MontyPool> Function(PoolOptions options);

/// How many workers the runner's pools have started so far.
typedef SpawnCount = int Function();

/// The running probe's [SpawnCount].
SpawnCount _spawned = () => 0;

/// One transcript event.
typedef _Ev = Map<String, Object?>;

/// What a probe the session cannot run yet waits for, by probe id.
const _pending = <String, String>{
  'sp_collect_string': _needsCollectors,
  'sp_collect_streams': _needsCollectors,
  'sp_collect_overflow': _needsCollectors,
};

const _needsCollectors = 'needs CollectString and CollectStreams (cutover)';

/// One byte past the frame limit (256 MiB): run_probes.py's `OVERSIZED`.
const int _oversized = 256 * 1024 * 1024 + 1;

/// run_probes.py's `EXT_RESULT_CODE`.
const _extResultCode =
    'try:\n    r = f()\nexcept ValueError as e:\n'
    '    r = "caught ValueError: " + str(e)\n'
    'except NameError as e:\n    r = "caught NameError: " + str(e)\nr';

final class _T {
  final events = <_Ev>[];

  void add(_Ev e) => events.add(e);
}

/// Runs probe [id] on [backend]: [shared] is the run's one-worker pool,
/// [start] opens any pool a probe configures itself, and [spawned] counts
/// the workers [start]'s pools have started.
Future<Map<String, Object?>> runSessionProbe(
  String id,
  String backend,
  MontyPool shared,
  StartPool start,
  SpawnCount spawned,
) async {
  _spawned = spawned;
  final out = <String, Object?>{
    'probe': id,
    'engine': 'session',
    'backend': backend,
    'events': const <Object?>[],
    'runner_error': null,
  };
  if (_pending[id] case final String why) return out..['unimplemented'] = why;
  final body = _probes[id];
  if (body == null) {
    return out..['runner_error'] = 'no session probe named $id';
  }
  final t = _T();
  try {
    await body(t, shared, start);
  } on Object catch (e, st) {
    out['runner_error'] = '$e\n$st';
  }

  return out..['events'] = t.events;
}

/// Every probe id this engine knows: the ones it runs and the pending ones.
Set<String> get sessionProbeIds => {..._probes.keys, ..._pending.keys};

final Map<
  String,
  Future<void> Function(_T t, MontyPool shared, StartPool start)
>
_probes = {
  // ------------------------------------------- suspensions, step by step
  'k_function': (t, shared, _) => shared.withSession((s) async {
    final x = await _step(t, () => s.feedStart("g = 1\ny = f(2, k='é')\ny"));
    await _step(t, () => _fn(x).resume(const ExternalReturnValue(MontyInt(5))));
  }),
  'k_os': (t, shared, _) => shared.withSession((s) async {
    final x = await _step(t, () => s.feedStart(_readCode));
    await _step(
      t,
      () => _fn(x).resume(const ExternalReturnValue(MontyString('hi'))),
    );
  }),
  'k_os_not_handled': (t, shared, _) => shared.withSession((s) async {
    final x = await _step(t, () => s.feedStart(_readCode));
    await _step(t, () => _fn(x).resumeNotHandled());
  }),
  'k_name': (t, shared, _) => shared.withSession((s) async {
    final x = await _step(t, () => s.feedStart('total = 1 + missing\ntotal'));
    await _step(
      t,
      () => (x! as NameLookupSnapshot).resume(value: const MontyInt(41)),
    );
  }),
  'k_future': (t, shared, _) => shared.withSession((s) async {
    final x = await _step(
      t,
      () => s.feedStart(
        'async def m():\n    a = await f(1)\n    return a + 1\nawait m()',
      ),
    );
    final y = await _step(t, () => _fn(x).resume(const ExternalFuture()));
    final futures = y! as FutureSnapshot;
    await _step(
      t,
      () => futures.resume({
        futures.pendingCallIds.first: const ExternalReturnValue(MontyInt(9)),
      }),
    );
  }),
  'pos_utf8': (t, shared, _) => shared.withSession(
    (s) => _step(t, () => s.feedStart("x = 'é'\ny = add(x, 2)")),
  ),
  'pos_earlier_feed': (t, shared, _) => shared.withSession((s) async {
    await s.feedRun(
      'def helper(n):\n    return fetch(n)',
      printCallback: _quiet,
    );
    await _step(t, () => s.feedStart('helper(3)'));
  }),
  'pos_eval': (t, shared, _) => shared.withSession(
    (s) => _step(t, () => s.feedStart("eval('  1 + fetch()')")),
  ),
  'inputs_typed': (t, shared, _) => shared.withSession((s) async {
    const shared = MontyList([MontyInt(1)]);
    final x = await _step(
      t,
      () => s.feedStart(
        'f(b, n, s)\n',
        inputs: {
          'b': MontyBytes(Uint8List.fromList([0, 255])),
          'n': MontyBigInt(BigInt.two.pow(70)),
          's': const MontyList([shared, shared]),
        },
      ),
    );
    await _step(t, () => _fn(x).resume(const ExternalReturnValue(MontyNone())));
  }),
  'auto_interleave': (t, _, start) => _withPool(
    start,
    const PoolOptions(maxProcesses: 2),
    (p) async {
      ProtoExternalFunction handler(String who, int times) => (args, kwargs) {
        t.add({
          'ev': 'handler',
          'who': who,
          'args': [identityCanon(args.first)],
        });

        return MontyInt((args.first as MontyInt).value * times);
      };
      final s1 = await p.checkout();
      final s2 = await p.checkout();
      try {
        const code = 'r = [h(1), h(2)]\nr';
        final a = await _step(
          t,
          () => s1.feedStart(code, externals: {'h': handler('A', 10)}),
        );
        final b = await _step(
          t,
          () => s2.feedStart(code, externals: {'h': handler('B', 100)}),
        );
        final a2 = await _step(t, () => _fn(a).resumeAuto());
        // Spent: refused before the handler runs.
        await _step(t, () => _fn(a).resumeAuto());
        var b2 = await _step(t, () => _fn(b).resumeAuto());
        await _step(
          t,
          () => _fn(a2).resume(const ExternalReturnValue(MontyInt(7))),
        );
        while (b2 != null && b2 is! SessionComplete) {
          final next = b2;
          b2 = await _step(t, () => _fn(next).resumeAuto());
        }
      } finally {
        await s1.close();
        await s2.close();
      }
    },
  ),
  'async_gather': (t, shared, _) => shared.withSession(
    (s) => _run(
      t,
      () => s.feedRun(
        'import asyncio\nawait asyncio.gather(af(1), af(2))',
        printCallback: _quiet,
        externals: {
          'af': coroutine((args, kwargs) async {
            final n = (args.first as MontyInt).value;
            t.add({
              'ev': 'handler',
              'who': 'af',
              'args': [identityCanon(args.first)],
            });
            await Future<void>.delayed(Duration(milliseconds: 10 * (3 - n)));

            return MontyInt(n * 10);
          }),
        },
      ),
    ),
  ),
  // Gathered async host functions run concurrently: each waits until both
  // have started (a barrier with a 10 s watchdog), which a driver that
  // awaited one call before starting the next could never pass.
  'sp_async_gather_overlap': (t, shared, _) => shared.withSession((s) {
    final started = <int>[];
    final both = Completer<void>();

    return _run(
      t,
      () => s.feedRun(
        'import asyncio\nawait asyncio.gather(af(1), af(2))',
        printCallback: _quiet,
        externals: {
          'af': coroutine((args, kwargs) async {
            final n = (args.first as MontyInt).value;
            started.add(n);
            t.add({
              'ev': 'handler',
              'who': 'af',
              'args': [identityCanon(args.first)],
            });
            if (started.length == 2) both.complete();
            await both.future.timeout(const Duration(seconds: 10));

            return MontyInt(n * 10);
          }),
        },
      ),
    );
  }),
  // A coroutine resumed automatically: awaited in place when the sandbox
  // awaits the call at once, its value or exception delivered; a call
  // stored first gets a pending future, settled when it is awaited.
  'sp_eager_coroutine_auto': (t, shared, _) => shared.withSession((s) async {
    final fns = <String, ProtoExternalFunction>{
      'f': coroutine((args, kwargs) async {
        await Future<void>.delayed(const Duration(milliseconds: 10));

        return const MontyInt(5);
      }),
      'g': coroutine(
        (args, kwargs) async =>
            throw const ExternalException('ValueError', 'bad'),
      ),
    };
    for (final (code, name) in _eagerAutoCodes) {
      try {
        var x = await s.feedStart(code, externals: {name: fns[name]!});
        t.add({'ev': 'eager', 'value': _fn(x).allowEagerAwait});
        while (x is! SessionComplete) {
          x = await _resumeAuto(x);
          if (x is! SessionComplete) {
            t.add({
              'ev': 'suspend',
              'kind': x is FutureSnapshot ? 'future' : 'call',
            });
          }
        }
        t.add({'ev': 'complete', 'value': identityCanon(x.output)});
      } on MontyRuntimeError catch (e) {
        t.add(_errEv(e.exception, e.traceback));
      } on MontySyntaxError catch (e) {
        t.add(_errEv(e.exception, e.traceback));
      }
    }
  }),
  // Gathered asyncio.sleeps overlap: every task has started its sleep
  // before any sleep ends (sleeps waited one after another would give [3],
  // [3, 1], [3, 1, 2]), and the gather waits at least its longest sleep, a
  // lower bound no scheduling delay can break.
  'sp_async_sleep_order': (t, shared, _) => shared.withSession((s) async {
    final clock = Stopwatch()..start();
    try {
      final value = await s.feedRun(
        'import asyncio\nstarted = []\nasync def a(n):\n'
        '    started.append(n)\n    await asyncio.sleep(0.4 * n)\n'
        '    return list(started)\nawait asyncio.gather(a(3), a(1), a(2))',
        printCallback: _quiet,
      );
      final took = clock.elapsed;
      t.add({
        'ev': 'complete',
        'value': identityCanon(value),
        'waited': took >= const Duration(milliseconds: 1100),
      });
    } on MontyRuntimeError catch (e) {
      t.add(_errEv(e.exception, e.traceback));
    } on MontySyntaxError catch (e) {
      t.add(_errEv(e.exception, e.traceback));
    }
  }),
  'abortctl_future': (t, shared, _) => shared.withSession((s) async {
    await s.feedRun('g = 1', printCallback: _quiet);
    var x = await _step(
      t,
      () => s.feedStart(
        'async def m():\n    try:\n        return await f(1)\n'
        '    except BaseException as e:\n        return "caught"\nawait m()',
      ),
    );
    x = await _step(t, () => _fn(x).resume(const ExternalFuture()));
    final futures = x! as FutureSnapshot;
    await _step(
      t,
      () => futures.resume({
        futures.pendingCallIds.first: const ExternalException(
          'RuntimeError',
          'aborted',
        ),
      }),
    );
    await _step(t, () => s.feedStart('g'));
  }),
  'abandon': (t, shared, _) => shared.withSession((s) async {
    final x = await _step(t, () => s.feedStart('g = 2\nf(1)'));
    await _step(t, () => s.feedStart('g'));
    await _step(t, () => _fn(x).resume(const ExternalReturnValue(MontyInt(5))));
    await _step(t, () => s.feedStart('g'));
  }),
  // ------------------------------------------- results and arguments
  'api_ext_result_resume': (t, shared, _) => shared.withSession((s) async {
    Future<void> complete(Future<MontySnapshot> Function() f) async => t.add({
      'ev': 'complete',
      'value': identityCanon((await f() as SessionComplete).output),
    });
    await complete(
      () async => _fn(
        await s.feedStart(_extResultCode),
      ).resume(const ExternalReturnValue(MontyInt(5))),
    );
    await complete(
      () async => _fn(
        await s.feedStart(_extResultCode),
      ).resume(const ExternalException('ValueError', 'bad')),
    );
    final snap = _fn(await s.feedStart('r = await f()\nr'));
    final fut = await snap.resume(const ExternalFuture()) as FutureSnapshot;
    await complete(
      () => fut.resume({snap.callId: const ExternalReturnValue(MontyInt(7))}),
    );
    await complete(
      () async =>
          _fn(await s.feedStart(_extResultCode, externals: {})).resumeAuto(),
    );
  }),
  'api_ext_args_values': (t, shared, _) => shared.withSession((s) async {
    final snap = _fn(await s.feedStart(_extArgsCode));
    t.add({
      'ev': 'suspend',
      'name': snap.functionName,
      'args': [for (final a in snap.args) identityCanon(a)],
      'kwargs': {
        for (final MapEntry(:key, :value) in snap.kwargs.entries)
          key: identityCanon(value),
      },
    });
    await snap.resume(const ExternalReturnValue(MontyNone()));
  }),
  'api_ext_args_identity': (t, shared, _) => shared.withSession((s) async {
    final snap = _fn(await s.feedStart(_extArgsCode));
    t.add({
      'ev': 'identity',
      'shared': identical(snap.args[2], snap.args[3]),
    });
    await snap.resume(const ExternalReturnValue(MontyNone()));
  }),
  'api_futures_gather': (t, shared, _) => shared.withSession((s) async {
    var snap = await s.feedStart(
      'import asyncio\nr = await asyncio.gather(f(1), f(2))\nr',
    );
    final ids = <int>[];
    while (snap is FunctionSnapshot) {
      ids.add(snap.callId);
      snap = await snap.resume(const ExternalFuture());
    }
    final futures = snap as FutureSnapshot;
    t.add({'ev': 'pending', 'count': futures.pendingCallIds.length});
    final done = await futures.resume({
      ids[0]: const ExternalReturnValue(MontyInt(10)),
      ids[1]: const ExternalReturnValue(MontyInt(20)),
    });
    t.add({
      'ev': 'complete',
      'value': identityCanon((done as SessionComplete).output),
    });
  }),
  'api_futures_error': (t, shared, _) => shared.withSession((s) async {
    final snap = _fn(
      await s.feedStart(
        'try:\n    r = await f(1)\nexcept ValueError as e:\n'
        '    r = "caught ValueError: " + str(e)\n'
        'except RuntimeError as e:\n'
        '    r = "caught RuntimeError: " + str(e)\nr',
      ),
    );
    final fut = await snap.resume(const ExternalFuture()) as FutureSnapshot;
    final done = await fut.resume({
      snap.callId: const ExternalException('ValueError', 'boom'),
    });
    t.add({
      'ev': 'complete',
      'value': identityCanon((done as SessionComplete).output),
    });
  }),
  'api_eager_coroutine': (t, shared, _) => shared.withSession((s) async {
    final a = _fn(await s.feedStart('r = f()\nr'));
    t.add({'ev': 'eager', 'sync_call': a.allowEagerAwait});
    await a.resume(const ExternalReturnValue(MontyInt(1)));
    final b = _fn(await s.feedStart('r = await f()\nr'));
    t.add({'ev': 'eager', 'await_call': b.allowEagerAwait});
  }),
  'sp_futures_partial': (t, shared, _) => shared.withSession((s) async {
    var x = await _step(
      t,
      () => s.feedStart(
        'import asyncio\nr = await asyncio.gather(f(1), f(2))\nr',
      ),
    );
    final ids = [_fn(x).callId];
    x = await _step(t, () => _fn(x).resume(const ExternalFuture()));
    ids.add(_fn(x).callId);
    // Upstream reports pending ids in hash order: compared sorted.
    final y = await _fn(x).resume(const ExternalFuture()) as FutureSnapshot;
    t.add({
      'ev': 'futures',
      'pending': [...y.pendingCallIds]..sort(),
    });
    final z =
        await y.resume({ids.last: const ExternalReturnValue(MontyInt(20))})
            as FutureSnapshot;
    t.add({
      'ev': 'futures',
      'pending': [...z.pendingCallIds]..sort(),
    });
    await _step(
      t,
      () => z.resume({ids.first: const ExternalReturnValue(MontyInt(10))}),
    );
  }),
  'sp_resume_validated': (t, shared, _) => shared.withSession((s) async {
    final x = await _step(t, () => s.feedStart('f()'));
    await _step(
      t,
      () => _fn(x).resume(const ExternalException('NotAnExceptionType')),
    );
    await _step(t, () => _fn(x).resume(const ExternalReturnValue(MontyInt(5))));
  }),
  'sp_resume_unconvertible': (t, shared, _) => shared.withSession((s) async {
    final x = await _step(t, () => s.feedStart('f()'));
    final items = <MontyValue>[];
    final cyclic = MontyList(items);
    items.add(cyclic);
    await _step(t, () => _fn(x).resume(ExternalReturnValue(cyclic)));
    await _step(t, () => _fn(x).resume(const ExternalReturnValue(MontyInt(5))));
  }),
  'sp_oversized_resume': (t, shared, _) => shared.withSession((s) async {
    final x = await _step(t, () => s.feedStart('f()'));
    await _step(
      t,
      () =>
          _fn(x).resume(ExternalReturnValue(MontyBytes(Uint8List(_oversized)))),
    );
    await _step(t, () => _fn(x).resume(const ExternalReturnValue(MontyInt(1))));
    _refusalKind(t);
    await _run(t, () => s.feedRun('3'));
    _refusalKind(t);
  }),
  'sp_checkout_script_name': (t, shared, _) => shared.withSession(
    (s) async {
      await _run(t, () => s.feedRun('__file__'));
      await _run(t, () => s.feedRun('def f():\n    return 1 / 0\nf()'));
    },
    options: const SessionConfig(scriptName: 'job.py'),
  ),
  // The second checkout gets the same worker back: one worker started in
  // all.
  'sp_finish_reset': (t, _, start) {
    final before = _spawned();

    return _withPool(start, const PoolOptions(maxProcesses: 1), (p) async {
      await p.withSession((s) => _run(t, () => s.feedRun('x = 1\nx')));
      await p.withSession((s) => _run(t, () => s.feedRun('x')));
      t.add({'ev': 'reused', 'same_worker': _spawned() - before == 1});
    });
  },
  // One worker, held: a checkout waits for it and gets it once it is
  // returned (a 10 s timeout, so no race); held again under a 0.3 s
  // timeout, a checkout waits it out and is refused.
  'sp_checkout_timeout': (t, _, start) async {
    await _withPool(
      start,
      const PoolOptions(
        maxProcesses: 1,
        checkoutTimeout: Duration(seconds: 10),
      ),
      (p) async {
        final s = await p.checkout();
        Future<void>? waiting;
        try {
          await _run(t, () => s.feedRun('1'));
          waiting = _enterTimed(t, p);
          await Future<void>.delayed(const Duration(milliseconds: 100));
          t.add({'ev': 'release'});
        } finally {
          await s.close();
        }
        await waiting;
      },
    );
    await _withPool(
      start,
      const PoolOptions(
        maxProcesses: 1,
        checkoutTimeout: Duration(milliseconds: 300),
      ),
      (p) => p.withSession((_) => _enterTimed(t, p)),
    );
  },
  // Two workers: two checkouts are held at once, a third waits until one
  // is returned.
  'sp_capacity_two': (t, _, start) {
    final before = _spawned();

    return _withPool(
      start,
      const PoolOptions(
        maxProcesses: 2,
        checkoutTimeout: Duration(seconds: 10),
      ),
      (p) async {
        final s1 = await p.checkout();
        final s2 = await p.checkout();
        Future<void>? waiting;
        try {
          await _run(t, () => s1.feedRun('1'));
          await _run(t, () => s2.feedRun('2'));
          waiting = p.withSession((s3) async {
            t.add({'ev': 'checkout'});
            await _run(t, () => s3.feedRun('3'));
          });
          await Future<void>.delayed(const Duration(milliseconds: 100));
          t.add({'ev': 'release'});
        } finally {
          await s2.close();
          await s1.close();
        }
        await waiting;
        // The third checkout ran on one of the two workers: no third was
        // started.
        t.add({'ev': 'workers', 'count': _spawned() - before});
      },
    );
  },
  'sp_pool_close': (t, _, start) async {
    final p = await start(const PoolOptions(maxProcesses: 2));
    final PoolSession s;
    try {
      s = await p.checkout();
    } on Object {
      await p.close();
      rethrow;
    }
    try {
      try {
        await _run(t, () => s.feedRun('x = 1'));
      } finally {
        await p.close();
      }
      await _run(t, () => s.feedRun('x + 1'));
      await _enter(t, p);
    } finally {
      await s.close();
    }
  },
  'sp_request_timeout': (t, _, start) => _withPool(
    start,
    // One slot, so the next checkout needs the killed worker's slot back.
    const PoolOptions(
      requestTimeout: Duration(milliseconds: 500),
      maxProcesses: 1,
      checkoutTimeout: Duration(seconds: 10),
    ),
    (p) async {
      await p.withSession((s) async {
        await _run(t, () => s.feedRun('sum(range(1000))'));
        // Host code is not a turn: a callback longer than the deadline is
        // not timed out.
        await _run(
          t,
          () => s.feedRun(
            'slow() + 1',
            externals: {
              'slow': (args, kwargs) async {
                await Future<void>.delayed(const Duration(milliseconds: 800));

                return const MontyInt(1);
              },
            },
          ),
        );
        final clock = Stopwatch()..start();
        try {
          await s.feedRun('while True:\n    pass');
          t.add({'ev': 'complete'});
          // The crash is the outcome measured here.
        } on MontyCrashedError {
          final at = clock.elapsed;
          t.add({
            'ev': 'crashed',
            'at_deadline':
                at >= const Duration(milliseconds: 400) &&
                at < const Duration(seconds: 5),
          });
        } on Object catch (e) {
          t.add({
            'ev': 'refused',
            'cls': e.runtimeType.toString(),
            'msg': '$e',
          });
        }
        await _run(t, () => s.feedRun('1'));
      });
      await p.withSession((s) => _run(t, () => s.feedRun('1 + 1')));
    },
  ),
  // The host function raises ValueError, the session's way today (core's
  // ExternalException naming the type); the sandbox catches it.
  'api_ext_result_callback': (t, shared, _) => shared.withSession(
    (s) => _run(
      t,
      () => s.feedRun(
        _extResultCode,
        externals: {
          'f': (args, kwargs) =>
              throw const ExternalException('ValueError', 'bad'),
        },
      ),
    ),
  ),
  'sp_oversized_input': (t, shared, _) => shared.withSession((s) async {
    await _run(
      t,
      () => s.feedRun(
        'len(x)',
        inputs: {'x': MontyBytes(Uint8List(_oversized))},
      ),
    );
    await _run(t, () => s.feedRun('1'));
  }),
  'sp_oversized_answer': (t, shared, _) => shared.withSession((s) async {
    await _run(
      t,
      () => s.feedRun(
        'f()',
        externals: {'f': (args, kwargs) => MontyBytes(Uint8List(_oversized))},
      ),
    );
    await _run(t, () => s.feedRun('2'));
  }),
  'sp_sync_sleep_waited': (t, shared, _) => shared.withSession((s) async {
    final clock = Stopwatch()..start();
    try {
      final value = await s.feedRun('import time\ntime.sleep(0.3)\n1');
      final waited = clock.elapsed;
      t.add({
        'ev': 'complete',
        'value': identityCanon(value),
        'waited':
            waited >= const Duration(milliseconds: 250) &&
            waited < const Duration(seconds: 5),
      });
    } on MontyRuntimeError catch (e) {
      t.add(_errEv(e.exception, e.traceback));
    } on MontySyntaxError catch (e) {
      t.add(_errEv(e.exception, e.traceback));
    }
  }),
  // With a long flush interval, output is held until a host call or the
  // end of the run, then delivered.
  'sp_print_batched': (t, shared, _) => shared.withSession(
    (s) => _run(
      t,
      () => s.feedRun(
        "print('a')\nprint('b')\nf()\nprint('c')\n4",
        printCallback: (stream, text) =>
            t.add({'ev': 'print', 'stream': stream, 'text': text}),
        externals: {
          'f': (args, kwargs) {
            t.add({'ev': 'handler', 'who': 'f', 'args': <Object?>[]});

            return const MontyInt(1);
          },
        },
      ),
    ),
    options: const SessionConfig(printFlushInterval: Duration(seconds: 10)),
  ),
  'sp_print_lines': (t, shared, _) => shared.withSession(
    (s) => _run(
      t,
      () => s.feedRun(
        "import sys\nprint('a')\nprint('b')\nprint('c', file=sys.stderr)\n"
        "print('d', end='')\n5",
        // Records only after a suspension of its own: a session that did
        // not await it would record the completion first, and one that ran
        // two at once records an overlap upstream never shows.
        printCallback: _serial(t, (stream, text) {
          t.add({'ev': 'print', 'stream': stream, 'text': text});
        }),
      ),
    ),
    options: const SessionConfig(printFlushInterval: Duration.zero),
  ),
  'sp_print_raise': (t, shared, _) => shared.withSession(
    (s) async {
      await s.feedRun('x = 1', printCallback: _quiet);
      await _failed(
        t,
        () => s.feedRun(
          "x = 2\nprint('a')\nx = 3\nprint('b')\n7",
          printCallback: _raising(t),
        ),
      );
      await _run(t, () => s.feedRun('x'));
    },
    options: const SessionConfig(printFlushInterval: Duration.zero),
  ),
  'sp_print_raise_then_error': (t, shared, _) => shared.withSession(
    (s) async {
      await _failed(
        t,
        () => s.feedRun(
          "print('a')\nraise ValueError('sandbox')",
          printCallback: _raising(t),
        ),
      );
      await _run(t, () => s.feedRun('9'));
    },
    options: const SessionConfig(printFlushInterval: Duration.zero),
  ),
  // ---------------------------------------------- dump and load (C1-5)
  'api_fork_session': (t, _, start) => _withPool(
    start,
    const PoolOptions(maxProcesses: 2),
    (p) async {
      final Uint8List bytes;
      final a = await p.checkout();
      try {
        await a.feedRun('x = [1]', printCallback: _quiet);
        bytes = await a.dump();
      } finally {
        await a.close();
      }
      final before = Uint8List.fromList(bytes);
      late MontyValue v2;
      late MontyValue v3;
      await p.withSession((s2) async {
        await s2.loadSession(bytes);
        await s2.feedRun('x.append(2)', printCallback: _quiet);
        v2 = await s2.feedRun('x', printCallback: _quiet);
      });
      await p.withSession((s3) async {
        await s3.loadSession(bytes);
        v3 = await s3.feedRun('x', printCallback: _quiet);
      });
      t.add({
        'ev': 'fork',
        'fork_a': identityCanon(v2),
        'fork_b': identityCanon(v3),
        'bytes_unchanged': _sameBytes(bytes, before),
      });
    },
  ),
  'sp_dump_idle': (t, _, start) => _withPool(
    start,
    const PoolOptions(maxProcesses: 2),
    (p) async {
      late Uint8List b;
      await p.withSession((a) async {
        await _run(
          t,
          () => a.feedRun(
            'x = [1]\ndef inc(n):\n    return n + x[0]\nx',
            printCallback: _quiet,
          ),
        );
        b = await a.dump();
        _dumped(t, b);
        await _run(
          t,
          () => a.feedRun('x.append(2)\nx', printCallback: _quiet),
        );
      });
      await p.withSession((c) async {
        await _tried(t, () => c.loadSession(b));
        await _run(
          t,
          () => c.feedRun('x.append(3)\n[inc(10), x]', printCallback: _quiet),
        );
      });
      await p.withSession((d) async {
        await _tried(t, () => d.loadSession(b));
        await _run(t, () => d.feedRun('[inc(10), x]', printCallback: _quiet));
      });
      late Uint8List fresh;
      await p.withSession((e) async {
        fresh = await e.dump();
        _dumped(t, fresh);
      });
      await p.withSession((f) async {
        await _tried(t, () => f.loadSession(fresh));
        await _run(t, () => f.feedRun('2 + 2', printCallback: _quiet));
      });
    },
  ),
  'sp_dump_suspended_function': (t, _, start) => _withPool(
    start,
    const PoolOptions(maxProcesses: 2),
    (p) async {
      await p.withSession((a) async {
        final x = await _step(
          t,
          () => a.feedStart("y = 5\nv = f(1, k='é')\n[v, y]"),
        );
        final snap = await _fn(x).dump();
        _dumped(t, snap);
        final mid = await a.dump();
        _dumped(t, mid);
        await p.withSession((b) async {
          final r = await _tried(t, () => b.loadSnapshot(snap));
          _announced(t, r);
          await _step(
            t,
            () => _fn(r).resume(const ExternalReturnValue(MontyInt(37))),
          );
          await _run(t, () => b.feedRun('[v, y]', printCallback: _quiet));
        });
        await p.withSession((c) async {
          final r2 = await _tried(t, () => c.loadSnapshot(mid));
          _announced(t, r2);
          await _step(
            t,
            () => _fn(r2).resume(const ExternalReturnValue(MontyInt(38))),
          );
        });
        await _step(
          t,
          () => _fn(x).resume(const ExternalReturnValue(MontyInt(1))),
        );
        await _run(t, () => a.feedRun('[v, y]', printCallback: _quiet));
      });
    },
  ),
  'sp_dump_suspended_kinds': (t, _, start) => _withPool(
    start,
    const PoolOptions(maxProcesses: 2),
    (p) async {
      await p.withSession((a) async {
        final x = await _step(
          t,
          () => a.feedStart('total = 1 + missing\ntotal'),
        );
        final state = await (x! as NameLookupSnapshot).dump();
        await p.withSession((b) async {
          final r = await _tried(t, () => b.loadSnapshot(state));
          _announced(t, r);
          await _step(
            t,
            () => (r! as NameLookupSnapshot).resume(value: const MontyInt(41)),
          );
        });
      });
      await p.withSession((a) async {
        final x = await _step(t, () => a.feedStart(_readCode));
        final state = await _fn(x).dump();
        await p.withSession((b) async {
          final r = await _tried(t, () => b.loadSnapshot(state));
          _announced(t, r);
          await _step(
            t,
            () => _fn(r).resume(const ExternalReturnValue(MontyString('hi'))),
          );
        });
      });
      await p.withSession((a) async {
        final x = await _step(
          t,
          () => a.feedStart(
            'async def m():\n    a = await f(1)\n    return a + 1\nawait m()',
          ),
        );
        final y = await _step(t, () => _fn(x).resume(const ExternalFuture()));
        final state = await (y! as FutureSnapshot).dump();
        await p.withSession((b) async {
          final r = await _tried(t, () => b.loadSnapshot(state));
          _announced(t, r);
          final futures = r! as FutureSnapshot;
          await _step(
            t,
            () => futures.resume({
              futures.pendingCallIds.first: const ExternalReturnValue(
                MontyInt(9),
              ),
            }),
          );
        });
      });
    },
  ),
  'sp_dump_spent_snapshot': (t, _, start) => _withPool(
    start,
    const PoolOptions(maxProcesses: 2),
    (p) async {
      await p.withSession((a) async {
        final x = await _step(t, () => a.feedStart('f(1)'));
        await _step(
          t,
          () => _fn(x).resume(const ExternalReturnValue(MontyInt(3))),
        );
        await _tried(t, () => _fn(x).dump());
        _refusalKind(t);
      });
      final b = await p.checkout();
      await b.feedRun('1', printCallback: _quiet);
      await b.close();
      await _tried(t, b.dump);
    },
  ),
  'sp_load_wrong_kind': (t, _, start) => _withPool(
    start,
    const PoolOptions(maxProcesses: 2),
    (p) async {
      late Uint8List suspended;
      late Uint8List idle;
      await p.withSession((a) async {
        final x = await _step(t, () => a.feedStart('f(1)'));
        suspended = await _fn(x).dump();
        await _step(
          t,
          () => _fn(x).resume(const ExternalReturnValue(MontyInt(1))),
        );
        await _run(t, () => a.feedRun('x = 1', printCallback: _quiet));
        idle = await a.dump();
      });
      await p.withSession((b) async {
        await _tried(t, () => b.loadSession(suspended));
        await _run(t, () => b.feedRun('1', printCallback: _quiet));
      });
      await p.withSession((c) async {
        await _tried(t, () => c.loadSnapshot(idle));
        await _run(t, () => c.feedRun('1', printCallback: _quiet));
      });
      await p.withSession((d) async {
        await _tried(t, () => d.loadSession(idle));
        await _run(t, () => d.feedRun('x', printCallback: _quiet));
      });
    },
  ),
  'sp_load_not_fresh': (t, _, start) => _withPool(
    start,
    const PoolOptions(maxProcesses: 2),
    (p) async {
      late Uint8List idle;
      late Uint8List suspended;
      await p.withSession((a) async {
        await _run(t, () => a.feedRun('x = 1', printCallback: _quiet));
        idle = await a.dump();
        final x = await _step(t, () => a.feedStart('f(1)'));
        suspended = await _fn(x).dump();
        await _step(
          t,
          () => _fn(x).resume(const ExternalReturnValue(MontyInt(1))),
        );
      });
      await p.withSession((b) async {
        await _run(t, () => b.feedRun('1', printCallback: _quiet));
        await _tried(t, () => b.loadSession(idle));
        await _tried(t, () => b.loadSnapshot(suspended));
        await _run(t, () => b.feedRun('2', printCallback: _quiet));
      });
      await p.withSession((c) async {
        await _tried(t, () => c.loadSession(idle));
        await _tried(t, () => c.loadSession(idle));
        await _tried(t, () => c.loadSnapshot(suspended));
        await _run(t, () => c.feedRun('x', printCallback: _quiet));
      });
    },
  ),
  'sp_load_garbage': (t, shared, _) async {
    final garbage = Uint8List.fromList('garbage'.codeUnits);
    await shared.withSession((a) async {
      await _tried(t, () => a.loadSession(garbage));
      await _run(t, () => a.feedRun('6', printCallback: _quiet));
    });
    await shared.withSession((b) async {
      await _tried(t, () => b.loadSnapshot(garbage));
      await _run(t, () => b.feedRun('7', printCallback: _quiet));
    });
  },
  'sp_load_adopts_dump': (t, _, start) => _withPool(
    start,
    const PoolOptions(maxProcesses: 2),
    (p) async {
      late Uint8List checked;
      late Uint8List plain;
      late Uint8List state;
      await p.withSession((a) async {
        await _run(t, () => a.feedRun('x: int = 1\nx', printCallback: _quiet));
        checked = await a.dump();
      }, options: const SessionConfig(typeCheck: true));
      await p.withSession((b) async {
        await _tried(t, () => b.loadSession(checked));
        await _run(
          t,
          () => b.feedRun('y: int = "a"\ny', printCallback: _quiet),
        );
      });
      await p.withSession((c) async => plain = await c.dump());
      await p.withSession((d) async {
        await _tried(t, () => d.loadSession(plain));
        await _run(
          t,
          () => d.feedRun('z: int = "a"\nz', printCallback: _quiet),
        );
      }, options: const SessionConfig(typeCheck: true));
      await p.withSession(
        (e) async {
          final x = await _step(
            t,
            () => e.feedStart('f(1)\nf(2)\nf(3)\n"done"'),
          );
          state = await _fn(x).dump();
        },
        options: const SessionConfig(limits: ResourceLimits(maxSuspensions: 5)),
      );
      for (final ceiling in <int?>[null, 0, 1, 2]) {
        await p.withSession(
          (g) async {
            var r = await _tried(t, () => g.loadSnapshot(state));
            _announced(t, r);
            for (var n = 0; n < 3; n++) {
              if (r == null || r is SessionComplete) break;
              final next = r;
              r = await _step(
                t,
                () => _fn(next).resume(ExternalReturnValue(MontyInt(n))),
              );
            }
            await _run(
              t,
              () => g.feedRun(
                'import time\ntime.sleep(0)\n7',
                printCallback: _quiet,
              ),
            );
          },
          options: ceiling == null
              ? const SessionConfig()
              : SessionConfig(limits: ResourceLimits(maxSuspensions: ceiling)),
        );
      }
    },
  ),
  'sp_print_raise_suspended': (t, shared, _) => shared.withSession(
    (s) async {
      await _failed(
        t,
        () => s.feedRun(
          "print('a')\nf(1)",
          printCallback: _raising(t),
          externals: {
            'f': (args, kwargs) {
              t.add({
                'ev': 'handler',
                'who': 'f',
                'args': [identityCanon(args.first)],
              });

              return args.first;
            },
          },
        ),
      );
      await _run(t, () => s.feedRun('8'));
    },
    options: const SessionConfig(printFlushInterval: Duration.zero),
  ),
};

/// Runs [body] with a pool [start] opens with [options], closing it after.
Future<void> _withPool(
  StartPool start,
  PoolOptions options,
  Future<void> Function(MontyPool pool) body,
) async {
  final pool = await start(options);
  try {
    await body(pool);
  } finally {
    await pool.close();
  }
}

const _readCode = "from pathlib import Path\nPath('/x').read_text()";

/// run_probes.py's `EXT_ARGS_CODE`.
const _extArgsCode = "a = [1]\nf(1, 'a', a, a, k=3, m={'x': 1.5}, n=None)";

/// A print callback for feeds whose prints a probe does not look at.
void _quiet(String stream, String text) {}

/// The snippets of `sp_eager_coroutine_auto`, each with the host function
/// it calls, as run_probes.py's `EAGER_AUTO_CODES`.
const _eagerAutoCodes = [
  ('r = await f()\nr', 'f'),
  (
    'try:\n    r = await g()\nexcept ValueError as e:\n'
        '    r = "caught " + str(e)\nr',
    'g',
  ),
  ('t = f()\nr = await t\nr', 'f'),
];

/// [snapshot] resumed automatically.
Future<MontySnapshot> _resumeAuto(MontySnapshot snapshot) => switch (snapshot) {
  FunctionSnapshot() => snapshot.resumeAuto(),
  FutureSnapshot() => snapshot.resumeAuto(),
  NameLookupSnapshot() => snapshot.resumeAuto(),
  SessionComplete() => throw StateError('the feed is complete'),
};

/// Records what the last event refused, as run_probes.py's `refusal_kind`:
/// a spent snapshot, a feed while a suspension is pending, or anything else.
void _refusalKind(_T t) {
  final last = t.events.isEmpty ? const <String, Object?>{} : t.events.last;
  final msg = last['ev'] == 'refused' ? '${last['msg']}' : '';
  final kind = msg.contains('already been resumed')
      ? 'spent'
      : msg.contains('suspension is awaiting an answer')
      ? 'pending'
      : 'other';
  t.add({'ev': 'refusal_kind', 'kind': kind});
}

/// [snapshot] as the function snapshot the probe expects.
FunctionSnapshot _fn(MontySnapshot? snapshot) => snapshot! as FunctionSnapshot;

/// Records what [step] reached, as run_probes.py's `T.step` does: the
/// snapshot or completion, a sandbox exception as `error`, anything else as
/// `refused`. Returns the snapshot, or null after an error.
Future<MontySnapshot?> _step(
  _T t,
  Future<MontySnapshot> Function() step,
) async {
  try {
    final snapshot = await step();
    t.add(_evOf(snapshot));

    return snapshot;
  } on MontyRuntimeError catch (e) {
    t.add(_errEv(e.exception, e.traceback));
  } on MontySyntaxError catch (e) {
    t.add(_errEv(e.exception, e.traceback));
  } on Object catch (e) {
    t.add({'ev': 'refused', 'cls': e.runtimeType.toString(), 'msg': '$e'});
  }

  return null;
}

/// run_probes.py's `ev_of`.
_Ev _evOf(MontySnapshot snapshot) => switch (snapshot) {
  SessionComplete(:final output) => {
    'ev': 'complete',
    'value': identityCanon(output),
  },
  FunctionSnapshot() => {
    'ev': 'suspend',
    'kind': snapshot.isOsFunction ? 'os' : 'function',
    'name': snapshot.functionName,
    'args': [for (final a in snapshot.args) identityCanon(a)],
    'kwargs': {
      for (final MapEntry(:key, :value) in snapshot.kwargs.entries)
        key: identityCanon(value),
    },
    'position': _pos(snapshot.position),
  },
  NameLookupSnapshot() => {
    'ev': 'suspend',
    'kind': 'name',
    'name': snapshot.variableName,
    'position': _pos(snapshot.position),
  },
  FutureSnapshot() => {
    'ev': 'suspend',
    'kind': 'future',
    'pending': snapshot.pendingCallIds,
    'position': _pos(snapshot.position),
  },
};

Map<String, Object?> _pos(SourceRange r) => {
  'filename': r.filename,
  'start': r.start,
  'end': r.end,
};

/// Records a feed's value as `complete`, or its error: a sandbox exception
/// as `error`, anything else as `refused`.
Future<void> _run(_T t, Future<MontyValue> Function() feed) async {
  try {
    t.add({'ev': 'complete', 'value': identityCanon(await feed())});
  } on MontyRuntimeError catch (e) {
    t.add(_errEv(e.exception, e.traceback));
  } on MontySyntaxError catch (e) {
    t.add(_errEv(e.exception, e.traceback));
  } on Object catch (e) {
    t.add({'ev': 'refused', 'cls': e.runtimeType.toString(), 'msg': '$e'});
  }
}

/// Records whether [pool] grants a checkout (returned at once) or refuses.
Future<void> _enter(_T t, MontyPool pool) async {
  final PoolSession s;
  try {
    s = await pool.checkout();
  } on Object catch (e) {
    t.add({'ev': 'refused', 'cls': e.runtimeType.toString(), 'msg': '$e'});

    return;
  }
  t.add({'ev': 'checkout'});
  await s.close();
}

/// Records whether [pool] grants a checkout (returned at once), or refuses
/// it after waiting its 300 ms timeout (between 250 ms and 2 s).
Future<void> _enterTimed(_T t, MontyPool pool) async {
  final clock = Stopwatch()..start();
  final PoolSession s;
  try {
    s = await pool.checkout();
  } on Object {
    final waited = clock.elapsed;
    t.add({
      'ev': 'checkout_refused',
      'waited':
          waited >= const Duration(milliseconds: 250) &&
          waited < const Duration(seconds: 2),
    });

    return;
  }
  t.add({'ev': 'checkout'});
  await s.close();
}

/// [record] run after a suspension of the callback's own, with an
/// `overlap` event first if another delivery is still running: the session
/// must await each delivery before the next.
ProtoPrintCallback _serial(_T t, void Function(String, String) record) {
  var running = 0;

  return (stream, text) async {
    if (running++ > 0) t.add({'ev': 'overlap'});
    try {
      await Future<void>.delayed(const Duration(milliseconds: 5));
      record(stream, text);
    } finally {
      running--;
    }
  };
}

/// A print callback that records its first delivery and throws `cb boom`,
/// each after a suspension of its own: the session must await it to see
/// either.
ProtoPrintCallback _raising(_T t) {
  final record = _serial(t, (stream, text) {
    t.add({'ev': 'print', 'stream': stream, 'text': text});
  });

  return (stream, text) async {
    await record(stream, text);
    await Future<void>.delayed(const Duration(milliseconds: 5));
    throw StateError('cb boom');
  };
}

/// A feed whose print callback throws: `failed`, with whether the error is
/// the callback's own.
Future<void> _failed(_T t, Future<MontyValue> Function() feed) async {
  try {
    t.add({'ev': 'complete', 'value': identityCanon(await feed())});
  } on Object catch (e) {
    t.add({'ev': 'failed', 'callback_error': '$e'.contains('cb boom')});
  }
}

/// run_probes.py's `dumped`: a dump gave opaque, non-empty bytes.
void _dumped(_T t, Uint8List state) =>
    t.add({'ev': 'dumped', 'bytes': state.isNotEmpty});

/// run_probes.py's `announced`: the suspension a load re-announced.
void _announced(_T t, MontySnapshot? s) =>
    t.add(s == null ? {'ev': 'none'} : _evOf(s));

bool _sameBytes(Uint8List a, Uint8List b) {
  if (a.length != b.length) return false;
  for (var i = 0; i < a.length; i++) {
    if (a[i] != b[i]) return false;
  }

  return true;
}

/// run_probes.py's `tried`: [call]'s result, or null with the refusal (or
/// the sandbox error) recorded.
Future<R?> _tried<R>(_T t, Future<R> Function() call) async {
  try {
    return await call();
  } on MontyRuntimeError catch (e) {
    t.add(_errEv(e.exception, e.traceback));
  } on MontySyntaxError catch (e) {
    t.add(_errEv(e.exception, e.traceback));
  } on Object catch (e) {
    t.add({'ev': 'refused', 'cls': e.runtimeType.toString(), 'msg': '$e'});
  }

  return null;
}

_Ev _errEv(MontyExceptionValue exception, List<Frame> traceback) {
  final last = traceback.isEmpty ? null : traceback.last;

  return {
    'ev': 'error',
    'type': exception.excType,
    'message': exception.message,
    'tb_last': last == null
        ? null
        : {'line': last.line, 'function': last.functionName},
  };
}
