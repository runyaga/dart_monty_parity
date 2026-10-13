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
import 'package:dart_monty_core/dart_monty_core.dart';
import 'package:dart_monty_core/src/worker/monty_pool.dart';
import 'package:dart_monty_core/src/worker/pool_session.dart';
import 'package:dart_monty_core/src/worker/proto_session.dart';
import 'package:dart_monty_core/src/worker/worker_pool.dart';
import 'package:monty_parity/src/worker_runner.dart';

/// Starts a pool with [options] for one probe; the probe closes it.
typedef StartPool = Future<MontyPool> Function(PoolOptions options);

/// One transcript event.
typedef _Ev = Map<String, Object?>;

/// What a probe the session cannot run yet waits for, by probe id.
const _pending = <String, String>{
  'k_function': _needsStart,
  'k_os': _needsStart,
  'k_os_not_handled': _needsStart,
  'k_name': _needsStart,
  'k_future': _needsStart,
  'abortctl_future': _needsStart,
  'pos_utf8': _needsStart,
  'pos_earlier_feed': _needsStart,
  'pos_eval': _needsStart,
  'inputs_typed': _needsStart,
  'auto_interleave': _needsStart,
  'abandon': _needsStart,
  'api_futures_gather': _needsStart,
  'api_futures_error': _needsStart,
  'api_ext_result_resume': _needsStart,
  'api_eager_coroutine': _needsStart,
  'api_ext_args_values': _needsStart,
  'api_ext_args_identity': _needsStart,
  'async_gather': 'needs futures in feedRun (C1-4)',
  'api_fork_session': 'needs dump and load (C1-5)',
};

const _needsStart = 'needs feedStart and snapshots (C1-4)';

/// run_probes.py's `EXT_RESULT_CODE`.
const _extResultCode =
    'try:\n    r = f()\nexcept ValueError as e:\n'
    '    r = "caught ValueError: " + str(e)\n'
    'except NameError as e:\n    r = "caught NameError: " + str(e)\nr';

final class _T {
  final events = <_Ev>[];

  void add(_Ev e) => events.add(e);
}

/// Runs probe [id] on [backend]: [shared] is the run's one-worker pool, and
/// [start] opens any pool a probe configures itself.
Future<Map<String, Object?>> runSessionProbe(
  String id,
  String backend,
  MontyPool shared,
  StartPool start,
) async {
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
  'sp_checkout_script_name': (t, shared, _) => shared.withSession(
    (s) async {
      await _run(t, () => s.feedRun('__file__'));
      await _run(t, () => s.feedRun('def f():\n    return 1 / 0\nf()'));
    },
    options: const SessionConfig(scriptName: 'job.py'),
  ),
  'sp_finish_reset': (t, _, start) => _withPool(
    start,
    const PoolOptions(maxProcesses: 1),
    (p) async {
      await p.withSession((s) => _run(t, () => s.feedRun('x = 1\nx')));
      await p.withSession((s) => _run(t, () => s.feedRun('x')));
    },
  ),
  // One worker, held: a checkout waits for it and gets it once it is
  // returned; held again, a checkout waits out the timeout and is refused.
  'sp_checkout_timeout': (t, _, start) => _withPool(
    start,
    const PoolOptions(
      maxProcesses: 1,
      checkoutTimeout: Duration(milliseconds: 300),
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
      await p.withSession((_) => _enterTimed(t, p));
    },
  ),
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
    const PoolOptions(requestTimeout: Duration(milliseconds: 500)),
    (p) async {
      await p.withSession((s) async {
        await _run(t, () => s.feedRun('while True:\n    pass'));
        await _run(t, () => s.feedRun('1'));
      });
      await p.withSession((s) => _run(t, () => s.feedRun('1 + 1')));
    },
  ),
  // The host function raises ValueError, the session's way today (core's
  // OsCallException naming the type); the sandbox catches it.
  'api_ext_result_callback': (t, shared, _) => shared.withSession(
    (s) => _run(
      t,
      () => s.feedRun(
        _extResultCode,
        externals: {
          'f': (args, kwargs) => throw const OsCallException(
            'bad',
            pythonExceptionType: 'ValueError',
          ),
        },
      ),
    ),
  ),
  'sp_print_lines': (t, shared, _) => shared.withSession(
    (s) => _run(
      t,
      () => s.feedRun(
        "import sys\nprint('a')\nprint('b')\nprint('c', file=sys.stderr)\n"
        "print('d', end='')\n5",
        printCallback: (stream, text) =>
            t.add({'ev': 'print', 'stream': stream, 'text': text}),
      ),
    ),
    options: const SessionConfig(printFlushInterval: Duration.zero),
  ),
  'sp_print_raise': (t, shared, _) => shared.withSession(
    (s) async {
      await _failed(
        t,
        () => s.feedRun(
          "print('a')\nprint('b')\n7",
          printCallback: _raising(t),
        ),
      );
      await _run(t, () => s.feedRun('8'));
    },
    options: const SessionConfig(printFlushInterval: Duration.zero),
  ),
  'sp_print_raise_suspended': (t, shared, _) => shared.withSession(
    (s) async {
      await _failed(
        t,
        () => s.feedRun(
          "print('a')\nf(1)",
          printCallback: _raising(t),
          externals: {'f': (args, kwargs) => args.first},
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

/// Records a feed's value as `complete`, or its error: a sandbox exception
/// as `error`, anything else as `refused`.
Future<void> _run(_T t, Future<MontyValue> Function() feed) async {
  try {
    t.add({'ev': 'complete', 'value': identityCanon(await feed())});
  } on MontyScriptError catch (e) {
    t.add(_errEv(e));
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
/// it after waiting at least 250 ms.
Future<void> _enterTimed(_T t, MontyPool pool) async {
  final clock = Stopwatch()..start();
  final PoolSession s;
  try {
    s = await pool.checkout();
  } on Object {
    t.add({
      'ev': 'checkout_refused',
      'waited': clock.elapsed >= const Duration(milliseconds: 250),
    });

    return;
  }
  t.add({'ev': 'checkout'});
  await s.close();
}

/// A print callback that records its first delivery and throws `cb boom`.
ProtoPrintCallback _raising(_T t) => (stream, text) {
  t.add({'ev': 'print', 'stream': stream, 'text': text});
  throw StateError('cb boom');
};

/// A feed whose print callback throws: `failed`, with whether the error is
/// the callback's own.
Future<void> _failed(_T t, Future<MontyValue> Function() feed) async {
  try {
    t.add({'ev': 'complete', 'value': identityCanon(await feed())});
  } on Object catch (e) {
    t.add({'ev': 'failed', 'callback_error': '$e'.contains('cb boom')});
  }
}

_Ev _errEv(MontyScriptError e) {
  final exception = e.exception;
  final last = exception == null || exception.traceback.isEmpty
      ? null
      : exception.traceback.last;

  return {
    'ev': 'error',
    'type': exception?.excType ?? e.excType,
    'message': exception?.message ?? e.message,
    'tb_last': last == null
        ? null
        : {'line': last.startLine, 'function': last.frameName},
  };
}
