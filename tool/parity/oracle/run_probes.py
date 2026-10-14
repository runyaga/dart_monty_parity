#!/usr/bin/env python3
"""Reference probes: API-level parity probes through upstream pydantic/monty 1.1.0.

usage: python -I tool/parity/oracle/run_probes.py OUTDIR

Probes cover what a single feed-and-compare case cannot: suspensions resumed by
hand, futures, async host functions, feeding while suspended, mounts and
callback files, session forks, the type checker. Each probe writes
OUTDIR/<probe>/ref-py.json = {probe, engine, backend, events, runner_error};
packages/monty_parity/lib/src/api_probes.dart emits the same events through
dart_monty_core, and tool/parity/probes_compare.py compares the two.

Event vocabulary: suspend{kind,name,args,kwargs,position,pending},
complete{value}, error{type,message,tb_last}, refused{}, handler{who,args},
unsupported{what}, plus probe-specific observation events; the pool and print
probes (`sp_*`) add print{stream,text}, checkout{}, checkout_refused{waited}, release{},
crashed{at_deadline}, failed{callback_error} and complete's waited.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

import pydantic_monty as pm

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_cases import canon, stable  # noqa: E402


def pos(s: Any) -> Any:
    return s.position.dict() if hasattr(s, 'position') else None


def ev_of(s: Any) -> dict[str, Any]:
    if isinstance(s, pm.MontyComplete):
        return {'ev': 'complete', 'value': canon(s.output)}
    if isinstance(s, pm.FunctionSnapshot):
        return {
            'ev': 'suspend',
            'kind': 'os' if s.is_os_function else 'function',
            'name': str(s.function_name),
            'args': [canon(a) for a in s.args],
            'kwargs': {k: canon(v) for k, v in s.kwargs.items()},
            'position': pos(s),
        }
    if isinstance(s, pm.NameLookupSnapshot):
        return {'ev': 'suspend', 'kind': 'name', 'name': s.variable_name, 'position': pos(s)}
    if isinstance(s, pm.FutureSnapshot):
        return {'ev': 'suspend', 'kind': 'future', 'pending': list(s.pending_call_ids), 'position': pos(s)}
    return {'ev': 'other', 'repr': repr(s)}


def err_ev(e: BaseException) -> dict[str, Any]:
    if isinstance(e, (pm.MontyRuntimeError, pm.MontySyntaxError)):
        msg = e.display('msg')
        tm = e.display('type-msg')
        typ = tm[: -(len(msg) + 2)] if msg and tm.endswith(': ' + msg) else tm.split(':', 1)[0]
        tb = e.traceback()
        last = tb[-1].dict() if tb else None
        return {
            'ev': 'error',
            'type': typ,
            'message': msg,
            'tb_last': None if last is None else {'line': last.get('line'), 'function': last.get('function_name')},
        }
    return {'ev': 'refused', 'cls': type(e).__name__, 'msg': str(e)}


def exc_obs(e: BaseException) -> dict[str, Any]:
    """An exception as type + message (the Python exception inside a pydantic_monty error)."""
    inner = e.exception() if isinstance(e, pm.MontyError) else e
    return {'type': type(inner).__name__, 'message': str(inner)}


class T:
    """Transcript recorder."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def add(self, e: dict[str, Any]) -> None:
        self.events.append(e)

    def step(self, fn: Any) -> Any:
        try:
            s = fn()
        except Exception as e:  # noqa: BLE001 - recorded
            self.add(err_ev(e))
            return None
        self.add(ev_of(s))
        return s


PROBES: dict[str, Any] = {}


def probe(fn: Any) -> Any:
    PROBES[fn.__name__] = fn
    return fn


# ------------------------------------------------------- suspensions (kinds, positions)
@probe
def k_function(pool: pm.Monty, t: T) -> None:
    with pool.checkout() as s:
        x = t.step(lambda: s.feed_start("g = 1\ny = f(2, k='é')\ny"))
        t.step(lambda: x.resume({'return_value': 5}))


@probe
def k_os(pool: pm.Monty, t: T) -> None:
    with pool.checkout() as s:
        x = t.step(lambda: s.feed_start("from pathlib import Path\nPath('/x').read_text()"))
        t.step(lambda: x.resume({'return_value': 'hi'}))


@probe
def k_os_not_handled(pool: pm.Monty, t: T) -> None:
    with pool.checkout() as s:
        x = t.step(lambda: s.feed_start("from pathlib import Path\nPath('/x').read_text()"))
        t.step(lambda: x.resume_not_handled())


@probe
def k_name(pool: pm.Monty, t: T) -> None:
    with pool.checkout() as s:
        x = t.step(lambda: s.feed_start('total = 1 + missing\ntotal'))
        t.step(lambda: x.resume(value=41))


@probe
def k_future(pool: pm.Monty, t: T) -> None:
    with pool.checkout() as s:
        x = t.step(lambda: s.feed_start('async def m():\n    a = await f(1)\n    return a + 1\nawait m()'))
        y = t.step(lambda: x.resume({'future': ...}))
        t.step(lambda: y.resume({y.pending_call_ids[0]: {'return_value': 9}}))


@probe
def pos_utf8(pool: pm.Monty, t: T) -> None:
    with pool.checkout() as s:
        t.step(lambda: s.feed_start("x = 'é'\ny = add(x, 2)"))


@probe
def pos_earlier_feed(pool: pm.Monty, t: T) -> None:
    with pool.checkout() as s:
        s.feed_run('def helper(n):\n    return fetch(n)')
        t.step(lambda: s.feed_start('helper(3)'))


@probe
def pos_eval(pool: pm.Monty, t: T) -> None:
    with pool.checkout() as s:
        t.step(lambda: s.feed_start("eval('  1 + fetch()')"))


@probe
def inputs_typed(pool: pm.Monty, t: T) -> None:
    shared = [1]
    with pool.checkout() as s:
        x = t.step(lambda: s.feed_start('f(b, n, s)\n', inputs={'b': b'\x00\xff', 'n': 2**70, 's': [shared, shared]}))
        t.step(lambda: x.resume({'return_value': None}))


@probe
def auto_interleave(pool: pm.Monty, t: T) -> None:
    def ha(n: int) -> int:
        t.add({'ev': 'handler', 'who': 'A', 'args': [canon(n)]})
        return n * 10

    def hb(n: int) -> int:
        t.add({'ev': 'handler', 'who': 'B', 'args': [canon(n)]})
        return n * 100

    with pool.checkout() as s1, pool.checkout() as s2:
        code = 'r = [h(1), h(2)]\nr'
        a = t.step(lambda: s1.feed_start(code, external_lookup={'h': ha}))
        b = t.step(lambda: s2.feed_start(code, external_lookup={'h': hb}))
        a2 = t.step(a.resume_auto)
        t.step(a.resume_auto)  # consumed: must refuse before the handler runs
        b2 = t.step(b.resume_auto)
        t.step(lambda: a2.resume({'return_value': 7}))  # manual step after auto
        while b2 is not None and not isinstance(b2, pm.MontyComplete):
            b2 = t.step(b2.resume_auto)


@probe
def async_gather(pool: pm.Monty, t: T) -> None:
    async def main() -> None:
        async def af(n: int) -> int:
            t.add({'ev': 'handler', 'who': 'af', 'args': [canon(n)]})
            await asyncio.sleep(0.01 * (3 - n))
            return n * 10

        async with pm.AsyncMonty() as apool, apool.checkout() as s:
            try:
                v = await s.feed_run('import asyncio\nawait asyncio.gather(af(1), af(2))', external_lookup={'af': af})
                t.add({'ev': 'complete', 'value': canon(v)})
            except Exception as e:  # noqa: BLE001
                t.add(err_ev(e))

    asyncio.run(main())


@probe
def abortctl_future(pool: pm.Monty, t: T) -> None:
    """A pending future resolved with an ordinary (catchable) error, then the session continues."""
    code = 'async def m():\n    try:\n        return await f(1)\n    except BaseException as e:\n        return "caught"\nawait m()'
    with pool.checkout() as s:
        s.feed_run('g = 1')
        x = t.step(lambda: s.feed_start(code))
        x = t.step(lambda: x.resume({'future': ...}))
        t.step(lambda: x.resume({x.pending_call_ids[0]: {'exc_type': 'RuntimeError', 'message': 'aborted'}}))
        t.step(lambda: s.feed_start('g'))


@probe
def abandon(pool: pm.Monty, t: T) -> None:
    with pool.checkout() as s:
        x = t.step(lambda: s.feed_start('g = 2\nf(1)'))
        t.step(lambda: s.feed_start('g'))  # feed while suspended
        t.step(lambda: x.resume({'return_value': 5}))  # the suspension survived
        t.step(lambda: s.feed_start('g'))


# ------------------------------------------------------- pool and print (the session engine's rows)
# Each `sp_*` probe starts the pools it needs, so pool settings and the reuse of
# one worker are part of what it measures. A feed's outcome is `complete` or an
# error event; an error that is not a sandbox exception (a timeout, a crash, a
# finished session) is `refused`, compared as a refusal.
def run(t: T, fn: Any) -> None:
    """Records a `feed_run`'s value as `complete`, or its error."""
    try:
        t.add({'ev': 'complete', 'value': canon(fn())})
    except Exception as e:  # noqa: BLE001 - recorded
        t.add(err_ev(e))


def enter(t: T, pool: pm.Monty) -> None:
    """Records whether a checkout is granted (`checkout`, then returned at once) or refused."""
    try:
        cm = pool.checkout()
        cm.__enter__()
    except Exception as e:  # noqa: BLE001 - recorded
        t.add(err_ev(e))
        return
    t.add({'ev': 'checkout'})
    cm.__exit__(None, None, None)


@probe
def sp_checkout_script_name(pool: pm.Monty, t: T) -> None:
    with pool.checkout(script_name='job.py') as s:
        run(t, lambda: s.feed_run('__file__'))
        run(t, lambda: s.feed_run('def f():\n    return 1 / 0\nf()'))


@probe
def sp_finish_reset(pool: pm.Monty, t: T) -> None:
    """One worker: the second checkout gets the same worker back, Reset, without the first one's globals."""
    with pm.Monty(max_processes=1) as p:
        with p.checkout() as s:
            run(t, lambda: s.feed_run('x = 1\nx'))
            first = s.worker_pid
        with p.checkout() as s:
            run(t, lambda: s.feed_run('x'))
            t.add({'ev': 'reused', 'same_worker': s.worker_pid == first})


@probe
def sp_checkout_timeout(pool: pm.Monty, t: T) -> None:
    """One worker, held: a checkout waits for it and gets it once it is returned (a 10 s timeout, so no race);
    held again under a 0.3 s timeout, a checkout waits it out and is refused."""
    async def enter_async(p: pm.AsyncMonty) -> None:
        start = time.monotonic()
        try:
            async with p.checkout():
                t.add({'ev': 'checkout'})
        except Exception:  # noqa: BLE001 - recorded: whether it waited its timeout, not the host's error type
            t.add({'ev': 'checkout_refused', 'waited': 0.25 <= time.monotonic() - start < 2})

    async def main() -> None:
        async with pm.AsyncMonty(max_processes=1, checkout_timeout=10) as p:
            async with p.checkout() as s:
                t.add({'ev': 'complete', 'value': canon(await s.feed_run('1'))})
                waiting = asyncio.ensure_future(enter_async(p))
                await asyncio.sleep(0.1)
                t.add({'ev': 'release'})
            await waiting
        async with pm.AsyncMonty(max_processes=1, checkout_timeout=0.3) as p:
            async with p.checkout():
                await enter_async(p)

    asyncio.run(main())


@probe
def sp_capacity_two(pool: pm.Monty, t: T) -> None:
    """Two workers: two checkouts are held at once, a third waits until one is returned."""
    async def main() -> None:
        async with pm.AsyncMonty(max_processes=2, checkout_timeout=10) as p:
            pids = set()
            async with p.checkout() as s1, p.checkout() as s2:
                t.add({'ev': 'complete', 'value': canon(await s1.feed_run('1'))})
                t.add({'ev': 'complete', 'value': canon(await s2.feed_run('2'))})
                pids |= {s1.worker_pid, s2.worker_pid}

                async def third() -> None:
                    async with p.checkout() as s3:
                        t.add({'ev': 'checkout'})
                        t.add({'ev': 'complete', 'value': canon(await s3.feed_run('3'))})
                        pids.add(s3.worker_pid)

                waiting = asyncio.ensure_future(third())
                await asyncio.sleep(0.1)
                t.add({'ev': 'release'})
            await waiting
            # The third checkout ran on one of the two workers: no third was started.
            t.add({'ev': 'workers', 'count': len(pids)})

    asyncio.run(main())


@probe
def sp_pool_close(pool: pm.Monty, t: T) -> None:
    """A session outlives its pool's close; the closed pool refuses a checkout."""
    p = pm.Monty(max_processes=2)
    p.__enter__()
    cm = p.checkout()
    s = cm.__enter__()
    try:
        run(t, lambda: s.feed_run('x = 1'))
    finally:
        p.__exit__(None, None, None)
    run(t, lambda: s.feed_run('x + 1'))
    enter(t, p)
    cm.__exit__(None, None, None)


@probe
def sp_request_timeout(pool: pm.Monty, t: T) -> None:
    """A turn within `request_timeout` (0.5 s) runs, and so does a host callback longer than it; a turn past it is
    killed at the deadline, as a crash, and finishes the session; the next checkout runs."""
    def slow() -> int:
        time.sleep(0.8)
        return 1

    # One slot, so the next checkout needs the killed worker's slot back.
    with pm.Monty(request_timeout=0.5, max_processes=1, checkout_timeout=10) as p:
        with p.checkout() as s:
            run(t, lambda: s.feed_run('sum(range(1000))'))
            # Host code is not a turn: a callback longer than the deadline is not timed out.
            run(t, lambda: s.feed_run('slow() + 1', external_lookup={'slow': slow}))
            start = time.monotonic()
            try:
                s.feed_run('while True:\n    pass')
                t.add({'ev': 'complete'})
            except pm.MontyCrashedError:
                t.add({'ev': 'crashed', 'at_deadline': 0.4 <= time.monotonic() - start < 5})
            except Exception as e:  # noqa: BLE001 - recorded
                t.add(err_ev(e))
            run(t, lambda: s.feed_run('1'))
        with p.checkout() as s:
            run(t, lambda: s.feed_run('1 + 1'))


@probe
def sp_sync_sleep_waited(pool: pm.Monty, t: T) -> None:
    """A system `time.sleep` is waited, not skipped: 0.3 s passes before the feed ends."""
    with pool.checkout() as s:
        start = time.monotonic()
        try:
            value = s.feed_run('import time\ntime.sleep(0.3)\n1')
        except Exception as e:  # noqa: BLE001 - recorded
            t.add(err_ev(e))
            return
        t.add({'ev': 'complete', 'value': canon(value), 'waited': 0.25 <= time.monotonic() - start < 5})


@probe
def sp_print_lines(pool: pm.Monty, t: T) -> None:
    """With no flush interval each line arrives on its own, in order, on its stream: two lines on one
    stream are two deliveries."""
    with pool.checkout(print_flush_interval=0) as s:
        def cb(stream: str, text: str) -> None:
            t.add({'ev': 'print', 'stream': stream, 'text': text})

        run(t, lambda: s.feed_run("import sys\nprint('a')\nprint('b')\nprint('c', file=sys.stderr)\n"
                                  "print('d', end='')\n5", print_callback=cb))


@probe
def sp_print_batched(pool: pm.Monty, t: T) -> None:
    """With a long flush interval, output is held until a host call or the end of the run, then delivered."""
    def f() -> int:
        t.add({'ev': 'handler', 'who': 'f', 'args': []})
        return 1

    with pool.checkout(print_flush_interval=10) as s:
        def cb(stream: str, text: str) -> None:
            t.add({'ev': 'print', 'stream': stream, 'text': text})

        run(t, lambda: s.feed_run("print('a')\nprint('b')\nf()\nprint('c')\n4", print_callback=cb,
                                  external_lookup={'f': f}))


@probe
def sp_collect_string(pool: pm.Monty, t: T) -> None:
    """`CollectString` gathers both streams into one string."""
    with pool.checkout() as s:
        c = pm.CollectString()
        run(t, lambda: s.feed_run("import sys\nprint('a')\nprint('b', file=sys.stderr)\n1", print_callback=c))
        t.add({'ev': 'collected', 'output': c.output})


@probe
def sp_collect_streams(pool: pm.Monty, t: T) -> None:
    """`CollectStreams` keeps (stream, text) pairs. With no flush interval each line is its own delivery, and
    upstream keeps each as its own pair (measured: adjacent stdout lines are not merged)."""
    with pool.checkout(print_flush_interval=0) as s:
        c = pm.CollectStreams()
        run(t, lambda: s.feed_run("import sys\nprint('a')\nprint('b')\nprint('c', file=sys.stderr)\n"
                                  "print('d')\n1", print_callback=c))
        t.add({'ev': 'collected', 'output': [list(p) for p in c.output]})


@probe
def sp_collect_overflow(pool: pm.Monty, t: T) -> None:
    """A collector past its cap fails the feed with `MemoryError`; the session stays usable."""
    with pool.checkout() as s:
        c = pm.CollectString(max_bytes=4)
        run(t, lambda: s.feed_run("print('hello world')\n1", print_callback=c))
        t.add({'ev': 'collected', 'output': c.output})
        run(t, lambda: s.feed_run('2'))


def _print_failure(t: T, code: str, **kw: Any) -> Any:
    """Feeds [code] with a print callback that raises `cb boom` at once; records the failure, not its type."""
    def bad(stream: str, text: str) -> None:
        t.add({'ev': 'print', 'stream': stream, 'text': text})
        raise ValueError('cb boom')

    return lambda s: _failed(t, lambda: s.feed_run(code, print_callback=bad, **kw))


def _failed(t: T, fn: Any) -> None:
    """A feed whose print callback raised: `failed` with whether its error carries the callback's text."""
    try:
        t.add({'ev': 'complete', 'value': canon(fn())})
    except Exception as e:  # noqa: BLE001 - recorded
        t.add({'ev': 'failed', 'callback_error': 'cb boom' in str(e)})


@probe
def sp_print_raise(pool: pm.Monty, t: T) -> None:
    """A raising print callback fails the feed, but its turn runs to the end: the session keeps what the feed did
    after the failed print."""
    with pool.checkout(print_flush_interval=0) as s:
        s.feed_run('x = 1')
        _print_failure(t, "x = 2\nprint('a')\nx = 3\nprint('b')\n7")(s)
        run(t, lambda: s.feed_run('x'))


@probe
def sp_async_sleep_order(pool: pm.Monty, t: T) -> None:
    """Gathered `asyncio.sleep`s overlap under the async binding. Each task records who has started by the time
    its sleep ends: all three ([3, 1, 2] three times), as when the sleeps run through the futures protocol. And
    the gather of 1.2 s, 0.4 s and 0.8 s sleeps takes about the longest, not the 2.4 s sum: it waits at least
    1.1 s and under 2 s, a 0.8 s margin either side for a slow runner."""
    code = ("import asyncio\nstarted = []\nasync def a(n):\n    started.append(n)\n"
            "    await asyncio.sleep(0.4 * n)\n    return list(started)\nawait asyncio.gather(a(3), a(1), a(2))")

    async def main() -> None:
        async with pm.AsyncMonty() as p, p.checkout() as s:
            start = time.monotonic()
            try:
                value = await s.feed_run(code)
            except Exception as e:  # noqa: BLE001 - recorded
                t.add(err_ev(e))
                return
            took = time.monotonic() - start
            t.add({'ev': 'complete', 'value': canon(value), 'waited': took >= 1.1, 'overlapped': took < 2.0})

    asyncio.run(main())


@probe
def sp_print_raise_then_error(pool: pm.Monty, t: T) -> None:
    """A raising print callback, then a sandbox exception in the same feed: the callback's failure wins, and the
    session stays usable."""
    with pool.checkout(print_flush_interval=0) as s:
        _print_failure(t, "print('a')\nraise ValueError('sandbox')")(s)
        run(t, lambda: s.feed_run('9'))


@probe
def sp_print_raise_suspended(pool: pm.Monty, t: T) -> None:
    """A raising print callback in a feed that then suspends: the feed fails, the call is never made, and the
    session is finished."""
    def f(x: Any) -> Any:
        t.add({'ev': 'handler', 'who': 'f', 'args': [canon(x)]})
        return x

    with pool.checkout(print_flush_interval=0) as s:
        _print_failure(t, "print('a')\nf(1)", external_lookup={'f': f})(s)
        run(t, lambda: s.feed_run('8'))


@probe
def sp_futures_partial(pool: pm.Monty, t: T) -> None:
    """Pending futures settle one at a time: the one left is reported again, then settles."""
    with pool.checkout() as s:
        x = t.step(lambda: s.feed_start('import asyncio\nr = await asyncio.gather(f(1), f(2))\nr'))
        ids = [x.call_id]
        x = t.step(lambda: x.resume({'future': ...}))
        ids.append(x.call_id)
        # Upstream reports pending ids in hash order, which varies run to run: compared sorted.
        y = x.resume({'future': ...})
        t.add({'ev': 'futures', 'pending': sorted(y.pending_call_ids)})
        z = y.resume({ids[1]: {'return_value': 20}})
        t.add({'ev': 'futures', 'pending': sorted(z.pending_call_ids)})
        t.step(lambda: z.resume({ids[0]: {'return_value': 10}}))


@probe
def sp_resume_validated(pool: pm.Monty, t: T) -> None:
    """An answer refused on its checks leaves the snapshot unspent: the same snapshot then resumes."""
    with pool.checkout() as s:
        x = t.step(lambda: s.feed_start('f()'))
        t.step(lambda: x.resume({'exc_type': 'NotAnExceptionType'}))
        t.step(lambda: x.resume({'return_value': 5}))


# One byte past the frame limit (MAX_FRAME_LEN, 256 MiB): a request carrying it cannot be sent.
OVERSIZED = 256 * 1024 * 1024 + 1


@probe
def sp_oversized_input(pool: pm.Monty, t: T) -> None:
    """An input too large to send fails the feed before it reaches the worker; the session stays usable."""
    with pool.checkout() as s:
        run(t, lambda: s.feed_run('len(x)', inputs={'x': b'\0' * OVERSIZED}))
        run(t, lambda: s.feed_run('1'))


@probe
def sp_oversized_answer(pool: pm.Monty, t: T) -> None:
    """A host function's answer too large to send fails the feed and leaves the call pending: the next feed is
    refused."""
    with pool.checkout() as s:
        run(t, lambda: s.feed_run('f()', external_lookup={'f': lambda: b'\0' * OVERSIZED}))
        run(t, lambda: s.feed_run('2'))


@probe
def sp_oversized_resume(pool: pm.Monty, t: T) -> None:
    """A manual resume too large to send fails, and the snapshot is spent: a retry and the next feed are refused."""
    with pool.checkout() as s:
        x = t.step(lambda: s.feed_start('f()'))
        t.step(lambda: x.resume({'return_value': b'\0' * OVERSIZED}))
        t.step(lambda: x.resume({'return_value': 1}))
        run(t, lambda: s.feed_run('3'))


# ------------------------------------------------------- API surface
@probe
def api_run_blocking(pool: pm.Monty, t: T) -> None:
    with pool.checkout() as s:
        t.add({'ev': 'complete', 'value': canon(s.feed_run('x * 2', inputs={'x': 21}))})
        try:
            s.feed_run('1 / 0')
        except pm.MontyError as e:
            t.add({'ev': 'error', **exc_obs(e)})
    with pool.checkout() as s:
        t.add({'ev': 'complete', 'value': canon(s.feed_run('x * 2', inputs={'x': 21}))})


@probe
def api_start_program(pool: pm.Monty, t: T) -> None:
    with pool.checkout() as s:
        snap = s.feed_start('y = fetch(1, k=2)\ny + 1')
        t.add({'ev': 'suspend', 'name': str(snap.function_name), 'args': [canon(a) for a in snap.args],
               'kwargs': {k: canon(v) for k, v in snap.kwargs.items()}})
        t.add({'ev': 'complete', 'value': canon(snap.resume({'return_value': 10}).output)})


@probe
def api_create_repl(pool: pm.Monty, t: T) -> None:
    with pool.checkout() as s:
        s.feed_run('x = 41\ndef inc(v):\n    return v + 1')
        t.add({'ev': 'complete', 'value': canon(s.feed_run('inc(x)'))})


@probe
def api_recover_repl_after_error(pool: pm.Monty, t: T) -> None:
    with pool.checkout() as s:
        s.feed_run('x = 1')
        for code in ('1 / 0', 'def r():\n    return r()\nr()', 'x = (', 'undefined_name'):
            try:
                s.feed_run(code)
                t.add({'ev': 'no_error', 'code': code})
            except pm.MontyError as e:
                t.add({'ev': 'error', 'code': code, **exc_obs(e), 'after': canon(s.feed_run('x + 1'))})


EXT_RESULT_CODE = ('try:\n    r = f()\nexcept ValueError as e:\n    r = "caught ValueError: " + str(e)\n'
                   'except NameError as e:\n    r = "caught NameError: " + str(e)\nr')


@probe
def api_ext_result_resume(pool: pm.Monty, t: T) -> None:
    """Low-level resume paths: a value, an exception, a future, not-found."""
    with pool.checkout() as s:
        t.add({'ev': 'complete', 'value': canon(s.feed_start(EXT_RESULT_CODE).resume({'return_value': 5}).output)})
        t.add({'ev': 'complete', 'value': canon(
            s.feed_start(EXT_RESULT_CODE).resume({'exc_type': 'ValueError', 'message': 'bad'}).output)})
        snap = s.feed_start('r = await f()\nr')
        fut = snap.resume({'future': ...})
        t.add({'ev': 'complete', 'value': canon(fut.resume({snap.call_id: {'return_value': 7}}).output)})
        t.add({'ev': 'complete', 'value': canon(s.feed_start(EXT_RESULT_CODE, external_lookup={}).resume_auto().output)})


@probe
def api_ext_result_callback(pool: pm.Monty, t: T) -> None:
    """A host callback that raises ValueError: the guest sees ValueError."""
    def boom(*a: Any, **k: Any) -> Any:
        raise ValueError('bad')

    with pool.checkout() as s:
        try:
            t.add({'ev': 'complete', 'value': canon(s.feed_run(EXT_RESULT_CODE, external_lookup={'f': boom}))})
        except pm.MontyError as e:
            t.add({'ev': 'error', **exc_obs(e)})


EXT_ARGS_CODE = "a = [1]\nf(1, 'a', a, a, k=3, m={'x': 1.5}, n=None)"


@probe
def api_ext_args_values(pool: pm.Monty, t: T) -> None:
    with pool.checkout() as s:
        snap = s.feed_start(EXT_ARGS_CODE)
        t.add({'ev': 'suspend', 'name': str(snap.function_name),
               'args': [canon(a) for a in snap.args], 'kwargs': {k: canon(v) for k, v in snap.kwargs.items()}})
        snap.resume({'return_value': None})


@probe
def api_ext_args_identity(pool: pm.Monty, t: T) -> None:
    with pool.checkout() as s:
        snap = s.feed_start(EXT_ARGS_CODE)
        t.add({'ev': 'identity', 'shared': snap.args[2] is snap.args[3]})
        snap.resume({'return_value': None})


@probe
def api_futures_gather(pool: pm.Monty, t: T) -> None:
    with pool.checkout() as s:
        snap = s.feed_start('import asyncio\nr = await asyncio.gather(f(1), f(2))\nr')
        ids = []
        while isinstance(snap, pm.FunctionSnapshot):
            ids.append(snap.call_id)
            snap = snap.resume({'future': ...})
        t.add({'ev': 'pending', 'count': len(snap.pending_call_ids)})
        t.add({'ev': 'complete', 'value': canon(
            snap.resume({ids[0]: {'return_value': 10}, ids[1]: {'return_value': 20}}).output)})


@probe
def api_futures_error(pool: pm.Monty, t: T) -> None:
    code = ('try:\n    r = await f(1)\nexcept ValueError as e:\n    r = "caught ValueError: " + str(e)\n'
            'except RuntimeError as e:\n    r = "caught RuntimeError: " + str(e)\nr')
    with pool.checkout() as s:
        snap = s.feed_start(code)
        cid = snap.call_id
        fut = snap.resume({'future': ...})
        t.add({'ev': 'complete', 'value': canon(fut.resume({cid: {'exc_type': 'ValueError', 'message': 'boom'}}).output)})


W_CODE = ("from pathlib import Path\nres = []\nfor n in ('a', 'b'):\n    try:\n"
          "        Path('/data/' + n + '.txt').write_text('xxxxxx')\n        res.append('ok')\n"
          "    except Exception as e:\n        res.append(type(e).__name__ + ': ' + str(e))\nres")
M_CODE = ("from pathlib import Path\nres = []\ndef w(n):\n    try:\n"
          "        Path('/data/' + n).write_text('y' * 300)\n        res.append('ok')\n"
          "    except Exception as e:\n        res.append(type(e).__name__ + ': ' + str(e))\n"
          "w('a')\nw('b')\nw('c')\nw('d')\ntry:\n    Path('/data/a').unlink()\n    res.append('unlinked')\n"
          "except Exception as e:\n    res.append(type(e).__name__ + ': ' + str(e))\nw('d')\nres")


def _write_budget(pool: pm.Monty, t: T, feed: int) -> None:
    with tempfile.TemporaryDirectory() as d, \
            pm.MountDir(host_path=Path(d), virtual_path='/data', write_bytes_limit=10) as m, pool.checkout() as s:
        for n in (1, 2):
            v = s.feed_run(W_CODE, mount=m)
            if n == feed:
                t.add({'ev': 'complete', 'value': canon(v)})


@probe
def api_write_budget_feed1(pool: pm.Monty, t: T) -> None:
    _write_budget(pool, t, 1)


@probe
def api_write_budget_feed2(pool: pm.Monty, t: T) -> None:
    _write_budget(pool, t, 2)


@probe
def api_memory_budget(pool: pm.Monty, t: T) -> None:
    with tempfile.TemporaryDirectory() as d, \
            pm.MountDir(host_path=Path(d), virtual_path='/data', memory_usage_limit=1000) as m, pool.checkout() as s:
        t.add({'ev': 'complete', 'value': canon(s.feed_run(M_CODE, mount=m))})


@probe
def api_callback_files(pool: pm.Monty, t: T) -> None:
    store = {'v': 'initial'}

    def rd(p: PurePosixPath) -> str:
        t.add({'ev': 'host', 'op': 'read', 'path': str(p)})
        return store['v']

    def wr(p: PurePosixPath, c: object) -> None:
        t.add({'ev': 'host', 'op': 'write', 'path': str(p), 'type': type(c).__name__, 'text': c})
        store['v'] = 'callback-saw:' + str(c)

    fs = pm.OSAccess([pm.CallbackFile('/cb/f.txt', read=rd, write=wr)])
    code = ("from pathlib import Path\np = Path('/cb/f.txt')\na = p.read_text()\n"
            "p.write_text('new')\nb = p.read_text()\n(a, b, p.exists())")
    with pool.checkout() as s:
        t.add({'ev': 'complete', 'value': canon(s.feed_run(code, os=fs))})


@probe
def api_boundary_uuid(pool: pm.Monty, t: T) -> None:
    with pool.checkout() as s:
        s.feed_run('class A:\n    pass\na1 = A()\na2 = A()')
        i1 = s.feed_run('a1')
        i1b = s.feed_run('a1')
        i2 = s.feed_run('a2')
        t.add({'ev': 'ids', 'is_uuid': isinstance(i1.id, uuid.UUID), 'stable': i1.id == i1b.id,
               'distinct': i1.id != i2.id})


@probe
def api_fork_session(pool: pm.Monty, t: T) -> None:
    with pool.checkout() as s:
        s.feed_run('x = [1]')
        b = s.dump()
    h = hashlib.sha256(b).hexdigest()
    with pool.checkout() as s2:
        s2.load_session(b)
        s2.feed_run('x.append(2)')
        v2 = s2.feed_run('x')
    with pool.checkout() as s3:
        s3.load_session(b)
        v3 = s3.feed_run('x')
    t.add({'ev': 'fork', 'fork_a': canon(v2), 'fork_b': canon(v3), 'bytes_unchanged': hashlib.sha256(b).hexdigest() == h})


@probe
def api_reset_type_checker(pool: pm.Monty, t: T) -> None:
    with pool.checkout(type_check=True) as s:
        s.feed_run('x: int = 1')
        try:
            s.feed_run('y: int = x\ny')
            t.add({'ev': 'checked', 'with_prior_definition': 'ok'})
        except pm.MontyError as e:
            t.add({'ev': 'checked', 'with_prior_definition': type(e).__name__})
    with pool.checkout(type_check=True) as s:
        try:
            s.feed_run('y: int = x\ny')
            t.add({'ev': 'checked', 'fresh_unresolved': False})
        except pm.MontyError as e:
            t.add({'ev': 'checked', 'fresh_unresolved': 'unresolved-reference' in str(e)})


@probe
def api_eager_coroutine(pool: pm.Monty, t: T) -> None:
    with pool.checkout() as s:
        a = s.feed_start('r = f()\nr')
        t.add({'ev': 'eager', 'sync_call': a.allow_eager_await})
        a.resume({'return_value': 1})
        b = s.feed_start('r = await f()\nr')
        t.add({'ev': 'eager', 'await_call': b.allow_eager_await})


def main() -> int:
    out = Path(sys.argv[1])
    only = set(sys.argv[2:])
    with pm.Monty() as pool:
        for name, fn in PROBES.items():
            if only and name not in only:
                continue
            t = T()
            err = None
            try:
                fn(pool, t)
            except Exception as e:  # noqa: BLE001
                err = ''.join(traceback.format_exception(e))
            d = out / name
            d.mkdir(parents=True, exist_ok=True)
            res = stable({'probe': name, 'engine': 'ref', 'backend': 'py', 'events': t.events, 'runner_error': err},
                         tempfile.gettempdir())
            (d / 'ref-py.json').write_text(json.dumps(res, indent=1, ensure_ascii=False) + '\n')
            print(f'ref {name}: {"ERROR" if err else "ok"}', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
