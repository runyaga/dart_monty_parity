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
import datetime
import hashlib
import json
import re
import struct
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
from host_cases import CASES as HOST_CASES, build as host_build, show as host_show  # noqa: E402


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
def sp_worker_recycling(pool: pm.Monty, t: T) -> None:
    """`max_checkouts_per_worker` retires a worker after that many checkouts: with 2, five checkouts of one slot run
    on three workers (new, same, new, same, new); with 0, each checkout gets a new one. No worker is started
    eagerly (`min_processes=0`), so each new one is the checkout's own."""
    for limit, n in ((2, 5), (0, 3)):
        with pm.Monty(min_processes=0, max_processes=1, max_checkouts_per_worker=limit, checkout_timeout=10) as p:
            prev = None
            for i in range(n):
                with p.checkout() as s:
                    run(t, lambda: s.feed_run(str(i)))
                    pid = s.worker_pid
                    t.add({'ev': 'worker', 'new': pid != prev})
                    prev = pid


@probe
def sp_worker_pid(pool: pm.Monty, t: T) -> None:
    """A session names its worker's process id, the same before and after a feed; two sessions held at once name
    two processes."""
    with pm.Monty(max_processes=2, checkout_timeout=10) as p:
        with p.checkout() as a, p.checkout() as b:
            first = a.worker_pid
            run(t, lambda: a.feed_run('1'))
            run(t, lambda: b.feed_run('2'))
            t.add({'ev': 'pid', 'is_int': isinstance(first, int) and first > 0, 'stable': a.worker_pid == first,
                   'distinct': isinstance(b.worker_pid, int) and b.worker_pid != first})


# One operation the sandbox cannot interrupt: about 3 s of big-integer arithmetic on an arm64 laptop, between two
# of the interpreter's time checks; longer than the default grace's kill (limit + 1 s), so a grace that was ignored
# shows as a late kill.
SLOW_POW = 'x = 3 ** 30_000_000\n1'


def finished_feed(t: T, fn: Any) -> None:
    """A feed on a session that should be finished: `finished` for the binding's finished-session refusal, its value
    or error otherwise, and any other refusal as `unexpected` with its class and text (a crash included)."""
    try:
        t.add({'ev': 'complete', 'value': canon(fn())})
    except (pm.MontyRuntimeError, pm.MontySyntaxError) as e:
        t.add(err_ev(e))
    except Exception as e:  # noqa: BLE001 - recorded
        if type(e) is RuntimeError and 'already been finished' in str(e):
            t.add({'ev': 'finished'})
        else:
            t.add({'ev': 'unexpected', 'cls': type(e).__name__, 'msg': str(e)})


@probe
def sp_execution_watchdogs(pool: pm.Monty, t: T) -> None:
    """A turn past its feed (or turn) limit that the sandbox cannot interrupt is killed once the pool's grace runs
    out too: a crash at the deadline (0.1 s limit + 0.1 s grace, so 0.2 s; under 0.8 s counts, where the default
    1 s grace would kill at 1.1 s), which finishes the session. With that grace off (None), the operation runs to its
    end and the sandbox raises the limit's TimeoutError, and the session lives."""
    for limit_key, grace_key in (('max_feed_duration_secs', 'feed_duration_limit_grace'),
                                 ('max_turn_duration_secs', 'turn_duration_limit_grace')):
        for grace in (0.1, None):
            with pm.Monty(max_processes=1, checkout_timeout=10, **{grace_key: grace}) as p:
                t.add({'ev': 'watchdog', 'limit': limit_key, 'grace': grace})
                with p.checkout(limits={limit_key: 0.1}) as s:
                    start = time.monotonic()
                    try:
                        t.add({'ev': 'complete', 'value': canon(s.feed_run(SLOW_POW))})
                    except pm.MontyCrashedError as e:
                        t.add({'ev': 'crashed', 'timed_out': e.timed_out,
                               'at_deadline': 0.15 <= time.monotonic() - start < 0.8})
                    except pm.MontyRuntimeError as e:
                        ev = err_ev(e)
                        # The limit's own text, without the measured duration.
                        t.add({**ev, 'message': ev['message'].split(':')[0]})
                    finished_feed(t, lambda: s.feed_run('2'))
                with p.checkout() as s:
                    run(t, lambda: s.feed_run('3'))


# Two 300,000-deep nested lists compared: the comparison recurses past the worker's native stack (a wasm stack
# overflow in the browser) when the recursion limit allows it, and the worker dies.
DEEP_COMPARE = ('x = []\nfor i in range(300000):\n    x = [x]\n'
                'y = []\nfor i in range(300000):\n    y = [y]\nx == y')


@probe
def sp_worker_crash(pool: pm.Monty, t: T) -> None:
    """A worker that dies mid-feed is reported as a crash, not a timeout, and finishes the session; the next
    checkout gets a working worker."""
    with pm.Monty(max_processes=1, checkout_timeout=10) as p:
        with p.checkout(limits={'max_memory': 512 * 1024 * 1024, 'max_recursion_depth': 100_000_000}) as s:
            try:
                t.add({'ev': 'complete', 'value': canon(s.feed_run(DEEP_COMPARE))})
            except pm.MontyCrashedError as e:
                t.add({'ev': 'crashed', 'timed_out': e.timed_out})
            except Exception as e:  # noqa: BLE001 - recorded
                t.add(err_ev(e))
            finished_feed(t, lambda: s.feed_run('2'))
        with p.checkout() as s:
            run(t, lambda: s.feed_run('3'))


@probe
def sp_hard_memory_ceiling(pool: pm.Monty, t: T) -> None:
    """An input far over a 1 MB memory limit trips the worker's hard allocator ceiling (limit + 4 MiB headroom):
    the worker exits with OOM_EXIT_CODE, which surfaces as MemoryError and finishes the session; the next checkout
    gets a working worker."""
    with pm.Monty(max_processes=1, checkout_timeout=10) as p:
        with p.checkout(limits={'max_memory': 1_000_000}) as s:
            run(t, lambda: s.feed_run('len(x)', inputs={'x': b'\0' * 10_000_000}))
            finished_feed(t, lambda: s.feed_run('2'))
        with p.checkout() as s:
            run(t, lambda: s.feed_run('3'))


@probe
def sp_memory_headroom(pool: pm.Monty, t: T) -> None:
    """R-P1/P7: decoded inputs, soft rejection versus hard death, unlimited allocation and reset accounting.

    The 2 MB input also distinguishes decoding the owned request buffer from making a second frame copy
    on the web. Both soft failures leave the input bound: the next feed raises MemoryError, not finished.
    A fresh one-worker pool keeps previous type-checking allocations out of these tight limits.
    """
    with pm.Monty(max_processes=1, checkout_timeout=10) as p:
        for size in (700_000, 1_500_000, 2_000_000, 6_000_000):
            t.add({'ev': 'input', 'bytes': size, 'max_memory': 1_000_000})
            with p.checkout(limits={'max_memory': 1_000_000}) as s:
                limit_error(t, lambda: s.feed_run('len(x)', inputs={'x': b'\0' * size}))
                if size == 6_000_000:
                    finished_feed(t, lambda: s.feed_run('2'))
                elif size > 1_000_000:
                    limit_error(t, lambda: s.feed_run('2'))
        t.add({'ev': 'no_limit'})
        with p.checkout() as s:
            run(t, lambda: s.feed_run("len('x' * 50_000_000)"))
        t.add({'ev': 'baseline', 'checkouts': 20, 'max_memory': 300_000})
        for _ in range(20):
            with p.checkout(limits={'max_memory': 300_000}) as s:
                run(t, lambda: s.feed_run('sum(range(1000))'))


def install(t: T, fn: Any) -> None:
    """An `install_dependencies` call: `installed`, a MontyRuntimeError as its error, and each binding refusal by
    its kind (a requirement with no UTF-8 form; a call while a suspension is unanswered); anything else as
    `unexpected` with its class and text."""
    try:
        fn()
        t.add({'ev': 'installed'})
    except pm.MontyRuntimeError as e:
        t.add(err_ev(e))
    except UnicodeEncodeError:
        t.add({'ev': 'unencodable'})
    except Exception as e:  # noqa: BLE001 - recorded
        if 'suspension is awaiting an answer' in str(e):
            t.add({'ev': 'pending'})
        else:
            t.add({'ev': 'unexpected', 'cls': type(e).__name__, 'msg': str(e)})


@probe
def sp_install_dependencies(pool: pm.Monty, t: T) -> None:
    """The pure-Monty worker installs nothing: an empty list succeeds; any requirement raises RuntimeError and the
    session runs on. Each requirement is checked first, in order: a blank one, or one starting with `-`, raises
    ValueError; one with no UTF-8 form is refused before any is checked. A call while a suspension is unanswered is
    refused, and the suspension then resumes."""
    with pool.checkout() as s:
        install(t, lambda: s.install_dependencies([]))
        install(t, lambda: s.install_dependencies(['httpx']))
        install(t, lambda: s.install_dependencies(['httpx', '  ']))
        install(t, lambda: s.install_dependencies([' -e x']))
        install(t, lambda: s.install_dependencies(['a', '\udc80']))
        run(t, lambda: s.feed_run('1'))
        x = t.step(lambda: s.feed_start('f()'))
        install(t, lambda: s.install_dependencies([]))
        if x is not None:
            t.step(lambda: x.resume({'return_value': 2}))


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
def sp_print_kill_after_print(pool: pm.Monty, t: T) -> None:
    """R-P1/P2: a print reaches the callback before a non-yielding turn is killed by the request timeout."""
    with pm.Monty(max_processes=1, checkout_timeout=10, request_timeout=1.5) as p:
        with p.checkout(print_flush_interval=0) as s:
            def cb(stream: str, text: str) -> None:
                t.add({'ev': 'print', 'stream': stream, 'text': text})

            try:
                t.add({'ev': 'complete', 'value': canon(s.feed_run(
                    "print('early')\nwhile True:\n    pass", print_callback=cb))})
            except pm.MontyCrashedError as e:
                t.add({'ev': 'crashed', 'timed_out': e.timed_out})
            except Exception as e:  # noqa: BLE001 - recorded
                t.add(err_ev(e))


@probe
def sp_print_volume_under_limit(pool: pm.Monty, t: T) -> None:
    """R-P1/P5: 4,020,000 printed bytes do not consume the worker's 2 MB memory limit."""
    with pm.Monty(max_processes=1, checkout_timeout=10) as p:
        with p.checkout(limits={'max_memory': 2_000_000}, print_flush_interval=0) as s:
            c = pm.CollectString(max_bytes=None)
            limit_error(t, lambda: s.feed_run("for i in range(20000):\n    print('y' * 200)\n'done'",
                                              print_callback=c))
            t.add({'ev': 'collected', 'bytes': len(c.output),
                   'content_matches': c.output == ('y' * 200 + '\n') * 20_000})


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
def sp_async_gather_overlap(pool: pm.Monty, t: T) -> None:
    """Gathered async host functions run concurrently: each waits until both have started (a barrier with a 10 s
    watchdog), which a driver that awaited one call before starting the next could never pass."""
    async def main() -> None:
        started: list[int] = []
        both = asyncio.Event()

        async def af(n: int) -> int:
            started.append(n)
            t.add({'ev': 'handler', 'who': 'af', 'args': [canon(n)]})
            if len(started) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 10)
            return n * 10

        async with pm.AsyncMonty() as p, p.checkout() as s:
            try:
                v = await s.feed_run('import asyncio\nawait asyncio.gather(af(1), af(2))', external_lookup={'af': af})
                t.add({'ev': 'complete', 'value': canon(v)})
            except Exception as e:  # noqa: BLE001 - recorded
                t.add(err_ev(e))

    asyncio.run(main())


@probe
def sp_eager_coroutine_auto(pool: pm.Monty, t: T) -> None:
    """A coroutine host function resumed automatically. A call the sandbox awaits at once (allow_eager_await) is
    awaited in place, its value or its exception delivered; one stored first gets a pending future, settled when
    the sandbox awaits it."""
    async def f() -> int:
        await asyncio.sleep(0.01)
        return 5

    async def g() -> int:
        raise ValueError('bad')

    async def main() -> None:
        async with pm.AsyncMonty() as p, p.checkout() as s:
            for code, ext in EAGER_AUTO_CODES:
                try:
                    x = await s.feed_start(code, external_lookup={ext: {'f': f, 'g': g}[ext]})
                    t.add({'ev': 'eager', 'value': x.allow_eager_await})
                    while not isinstance(x, pm.MontyComplete):
                        x = await x.resume_auto()
                        if not isinstance(x, pm.MontyComplete):
                            t.add({'ev': 'suspend', 'kind': 'future' if hasattr(x, 'pending_call_ids') else 'call'})
                    t.add({'ev': 'complete', 'value': canon(x.output)})
                except Exception as e:  # noqa: BLE001 - recorded
                    t.add(err_ev(e))

    asyncio.run(main())


EAGER_AUTO_CODES = (
    ('r = await f()\nr', 'f'),
    ('try:\n    r = await g()\nexcept ValueError as e:\n    r = "caught " + str(e)\nr', 'g'),
    ('t = f()\nr = await t\nr', 'f'),
)


@probe
def sp_async_sleep_order(pool: pm.Monty, t: T) -> None:
    """Gathered `asyncio.sleep`s overlap under the async binding. Each task records who has started by the time
    its sleep ends: all three ([3, 1, 2] three times), as when the sleeps run through the futures protocol;
    sleeps waited one after another would give [3], [3, 1], [3, 1, 2]. And the gather waits at least 1.1 s
    (its longest sleep is 1.2 s): a lower bound only, which no scheduling delay can break."""
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
            t.add({'ev': 'complete', 'value': canon(value), 'waited': took >= 1.1})

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


@probe
def sp_resume_unconvertible(pool: pm.Monty, t: T) -> None:
    """A manual resume with a value that cannot be converted fails on the host, and the snapshot stays unspent:
    the same snapshot then resumes (D1-calls rule 6)."""
    cyclic: list[Any] = []
    cyclic.append(cyclic)
    with pool.checkout() as s:
        x = t.step(lambda: s.feed_start('f()'))
        t.step(lambda: x.resume({'return_value': cyclic}))
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
    """A manual resume too large to send fails, and the snapshot is spent: a retry and the next feed are refused.
    Each refusal's kind is recorded, so a session that died or finished instead of staying suspended diverges."""
    with pool.checkout() as s:
        x = t.step(lambda: s.feed_start('f()'))
        t.step(lambda: x.resume({'return_value': b'\0' * OVERSIZED}))
        t.step(lambda: x.resume({'return_value': 1}))
        refusal_kind(t)
        run(t, lambda: s.feed_run('3'))
        refusal_kind(t)


def refusal_kind(t: T) -> None:
    """Records what the last event refused, by its message: a spent snapshot, a feed while a suspension is
    pending, or anything else."""
    last = t.events[-1] if t.events else {}
    msg = str(last.get('msg', '')) if last.get('ev') == 'refused' else ''
    kind = ('spent' if 'already been resumed' in msg else
            'pending' if 'suspension is awaiting an answer' in msg else
            'other')
    t.add({'ev': 'refusal_kind', 'kind': kind})


# ------------------------------------------------------- dump and load (C1-5)
def dumped(t: T, state: Any) -> None:
    """Records that a dump gave opaque, non-empty bytes (the bytes themselves are the worker's)."""
    t.add({'ev': 'dumped', 'bytes': isinstance(state, bytes) and len(state) > 0})


def tried(t: T, fn: Any) -> Any:
    """Calls fn and records a refusal; returns its result, or None when it was refused."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - recorded
        t.add(err_ev(e))
        return None


def typed(t: T, fn: Any) -> None:
    """A feed's value as `complete`, a type-check rejection as `typing_error` (a distinct event, so a session that
    merely refuses to run cannot pass for one that type-checks), any other error as usual."""
    try:
        t.add({'ev': 'complete', 'value': canon(fn())})
    except pm.MontyTypingError:
        t.add({'ev': 'typing_error'})
    except Exception as e:  # noqa: BLE001 - recorded
        t.add(err_ev(e))


def announced(t: T, s: Any) -> None:
    """Records the suspension a load re-announced, or that none came."""
    t.add(ev_of(s) if s is not None else {'ev': 'none'})


@probe
def sp_dump_idle(pool: pm.Monty, t: T) -> None:
    """Between feeds: globals and functions travel; a never-fed session dumps; the source runs on; one dump
    loaded twice gives two independent sessions."""
    with pool.checkout() as a:
        run(t, lambda: a.feed_run('x = [1]\ndef inc(n):\n    return n + x[0]\nx'))
        b = a.dump()
        dumped(t, b)
        run(t, lambda: a.feed_run('x.append(2)\nx'))
    with pool.checkout() as c:
        tried(t, lambda: c.load_session(b))
        run(t, lambda: c.feed_run('x.append(3)\n[inc(10), x]'))
    with pool.checkout() as d:
        tried(t, lambda: d.load_session(b))
        run(t, lambda: d.feed_run('[inc(10), x]'))
    with pool.checkout() as e:
        fresh = e.dump()
        dumped(t, fresh)
    with pool.checkout() as f:
        tried(t, lambda: f.load_session(fresh))
        run(t, lambda: f.feed_run('2 + 2'))


@probe
def sp_dump_suspended_function(pool: pm.Monty, t: T) -> None:
    """A suspension's dump (the snapshot's, or the session's while it waits) restores the same call on a fresh
    session; the original is unspent and runs on."""
    with pool.checkout() as a:
        x = t.step(lambda: a.feed_start("y = 5\nv = f(1, k='é')\n[v, y]"))
        snap = x.dump()
        dumped(t, snap)
        mid = a.dump()
        dumped(t, mid)
        with pool.checkout() as b:
            r = tried(t, lambda: b.load_snapshot(snap))
            announced(t, r)
            t.step(lambda: r.resume({'return_value': 37}))
            run(t, lambda: b.feed_run('[v, y]'))
        with pool.checkout() as c:
            r2 = tried(t, lambda: c.load_snapshot(mid))
            announced(t, r2)
            t.step(lambda: r2.resume({'return_value': 38}))
        t.step(lambda: x.resume({'return_value': 1}))
        run(t, lambda: a.feed_run('[v, y]'))


@probe
def sp_dump_suspended_kinds(pool: pm.Monty, t: T) -> None:
    """A name lookup, an OS call and a future suspension each dump, load and resume on a fresh session."""
    with pool.checkout() as a:
        x = t.step(lambda: a.feed_start('total = 1 + missing\ntotal'))
        state = x.dump()
        with pool.checkout() as b:
            r = tried(t, lambda: b.load_snapshot(state))
            announced(t, r)
            t.step(lambda: r.resume(value=41))
    with pool.checkout() as a:
        x = t.step(lambda: a.feed_start("from pathlib import Path\nPath('/x').read_text()"))
        state = x.dump()
        with pool.checkout() as b:
            r = tried(t, lambda: b.load_snapshot(state))
            announced(t, r)
            t.step(lambda: r.resume({'return_value': 'hi'}))
    with pool.checkout() as a:
        x = t.step(lambda: a.feed_start('async def m():\n    a = await f(1)\n    return a + 1\nawait m()'))
        y = t.step(lambda: x.resume({'future': ...}))
        state = y.dump()
        with pool.checkout() as b:
            r = tried(t, lambda: b.load_snapshot(state))
            announced(t, r)
            t.step(lambda: r.resume({r.pending_call_ids[0]: {'return_value': 9}}))


@probe
def sp_dump_spent_snapshot(pool: pm.Monty, t: T) -> None:
    """A snapshot that has been resumed cannot be dumped; a finished session dumps nothing."""
    with pool.checkout() as a:
        x = t.step(lambda: a.feed_start('f(1)'))
        t.step(lambda: x.resume({'return_value': 3}))
        tried(t, lambda: x.dump())
        refusal_kind(t)
    with pool.checkout() as b:
        b.feed_run('1')
    tried(t, lambda: b.dump())


@probe
def sp_load_wrong_kind(pool: pm.Monty, t: T) -> None:
    """A dump of the other kind is refused, and the session that took it is finished; the dump itself is intact."""
    with pool.checkout() as a:
        x = t.step(lambda: a.feed_start('f(1)'))
        suspended = x.dump()
        t.step(lambda: x.resume({'return_value': 1}))
        run(t, lambda: a.feed_run('x = 1'))
        idle = a.dump()
    with pool.checkout() as b:
        tried(t, lambda: b.load_session(suspended))
        run(t, lambda: b.feed_run('1'))
    with pool.checkout() as c:
        tried(t, lambda: c.load_snapshot(idle))
        run(t, lambda: c.feed_run('1'))
    with pool.checkout() as d:
        tried(t, lambda: d.load_session(idle))
        run(t, lambda: d.feed_run('x'))


@probe
def sp_load_not_fresh(pool: pm.Monty, t: T) -> None:
    """A load is only for a session never fed or loaded: after a feed or an earlier load it is refused."""
    with pool.checkout() as a:
        run(t, lambda: a.feed_run('x = 1'))
        idle = a.dump()
        x = t.step(lambda: a.feed_start('f(1)'))
        suspended = x.dump()
        t.step(lambda: x.resume({'return_value': 1}))
    with pool.checkout() as b:
        run(t, lambda: b.feed_run('1'))
        tried(t, lambda: b.load_session(idle))
        tried(t, lambda: b.load_snapshot(suspended))
        run(t, lambda: b.feed_run('2'))
    with pool.checkout() as c:
        tried(t, lambda: c.load_session(idle))
        tried(t, lambda: c.load_session(idle))
        tried(t, lambda: c.load_snapshot(suspended))
        run(t, lambda: c.feed_run('x'))


@probe
def sp_load_garbage(pool: pm.Monty, t: T) -> None:
    """Bytes the worker cannot load are refused, and the session runs on."""
    with pool.checkout() as a:
        tried(t, lambda: a.load_session(b'garbage'))
        run(t, lambda: a.feed_run('6'))
    with pool.checkout() as b:
        tried(t, lambda: b.load_snapshot(b'garbage'))
        run(t, lambda: b.feed_run('7'))


def with_version(dump: bytes, version: int) -> bytes:
    """[dump] with its format version (the little-endian u16 after the 6-byte `MONTY\\0` magic) replaced."""
    return dump[:6] + struct.pack('<H', version) + dump[8:]


DUMP_VERSIONS = (13, 12, 8, 14, 65535)


@probe
def sp_dump_version_window(pool: pm.Monty, t: T) -> None:
    """A dump carries its format version; a build loads only its own (13): an older one (12, or the 0.0.23
    baseline's 8) and a newer one (14, 65535) are refused with the version window in the message, and the session
    that refused it runs on. The same for a suspension's dump through `load_snapshot`."""
    with pool.checkout() as a:
        run(t, lambda: a.feed_run('x = 41'))
        idle = a.dump()
    t.add({'ev': 'version', 'kind': 'session', 'current': struct.unpack('<H', idle[6:8])[0]})
    for v in DUMP_VERSIONS:
        with pool.checkout() as s:
            t.add({'ev': 'load', 'version': v})
            tried(t, lambda: s.load_session(with_version(idle, v)))
            run(t, lambda: s.feed_run('x + 1' if v == 13 else '1'))
    with pool.checkout() as a:
        snap = a.feed_start('f()')
        suspended = snap.dump()
    t.add({'ev': 'version', 'kind': 'snapshot', 'current': struct.unpack('<H', suspended[6:8])[0]})
    for v in DUMP_VERSIONS:
        with pool.checkout() as s:
            t.add({'ev': 'load', 'version': v})
            x = tried(t, lambda: s.load_snapshot(with_version(suspended, v)))
            if x is not None:
                announced(t, x)
                t.step(lambda: x.resume({'return_value': 2}))
            run(t, lambda: s.feed_run('3'))


# A naive instant (UTC), so the zone alone decides the local time.
TZ_INSTANT = datetime.datetime(2024, 3, 1, 12, 30, 0)
TZ_CODE = ("import datetime, time\nn = datetime.datetime.now()\n"
           "a = datetime.datetime.now(datetime.timezone.utc).astimezone()\n"
           "(str(n), str(a), a.tzname(), str(a.utcoffset()), time.timezone, time.tzname, a.strftime('%Z %z'),"
           " str(datetime.date.today()))")
TZ_FIXED = ({'offset_seconds': 19800, 'name': 'IST'}, {'offset_seconds': -12600}, {'offset_seconds': 0},
            {'offset_seconds': -86399, 'name': 'x'}, {'offset_seconds': 3600, 'name': ''},
            {'offset_seconds': 3600, 'name': 'Europe/London'}, {'offset_seconds': 86400}, 'Asia/Kolkata', 'Mars/Olympus')


@probe
def sp_fixed_offset_timezone(pool: pm.Monty, t: T) -> None:
    """A fixed-offset `TimeZone` as the sandbox's zone: the local clock, `astimezone()`, `tzname`, `time.timezone`,
    `%Z %z` and the date follow the offset; without a name the zone is called `UTC+hh:mm`; a name is only a label
    (an IANA name included); an offset of a whole day or more is refused at checkout. An IANA name resolves with its
    rules, and an unknown one is refused at checkout with its own text."""
    for zone in TZ_FIXED:
        t.add({'ev': 'zone', **zone} if isinstance(zone, dict) else {'ev': 'zone', 'iana': zone})
        # Only the zone check's own refusal, with its text, counts; any other failure is recorded as it is.
        try:
            with pool.checkout(os_policy={'datetime': TZ_INSTANT, 'timezone': zone}) as s:
                run(t, lambda: s.feed_run(TZ_CODE))
        except ValueError as e:
            t.add({'ev': 'zone_refused', 'message': str(e)})
        except Exception as e:  # noqa: BLE001 - recorded
            t.add({'ev': 'unexpected', 'cls': type(e).__name__, 'msg': str(e)})


def limit_error(t: T, fn: Any) -> None:
    """As `run`, with the byte counts in a memory limit's message replaced by N (they are the worker's heap)."""
    run(t, fn)
    last = t.events[-1]
    if last.get('ev') == 'error':
        last['message'] = re.sub(r'\d+ bytes', 'N bytes', last['message'])


@probe
def sp_input_resource_failure(pool: pm.Monty, t: T) -> None:
    """An input the session's memory limit cannot hold, nested in a list too: the input converts and the feed's code
    starts (it prints first), then the limit's first check fails the feed with MemoryError. In 1.1.0 the import path
    (crates/monty/src/object_bridge.rs, import_node) allocates without a resource check, so InvalidInputError's
    resource arm, which the runner handles, is never produced from a binding's input. The session is not finished,
    but upstream keeps the failed input on its heap, so the next feed fails the same way; a small input runs on the
    next checkout. The probe has its own pool: a worker that served a type-checked session keeps about 3 MB that a
    later session's memory limit counts (upstream and core alike), so the shared worker's history would decide the
    outcome."""
    limits = pm.ResourceLimits(max_memory=70_000)
    code = "print('started')\nlen(x)"
    with pm.Monty(max_processes=1, checkout_timeout=10) as p:
        for x in ('a' * 60_000, ['b', ['c' * 60_000]]):
            with p.checkout(limits=limits) as s:
                printed: list[str] = []
                limit_error(t, lambda: s.feed_run(code, inputs={'x': x},
                                                  print_callback=lambda _stream, text: printed.append(text)))
                t.add({'ev': 'printed', 'text': ''.join(printed)})
                limit_error(t, lambda: s.feed_run('1'))
        with p.checkout(limits=limits) as s:
            limit_error(t, lambda: s.feed_run('len(x)', inputs={'x': 'a' * 10}))


@probe
def sp_load_adopts_dump(pool: pm.Monty, t: T) -> None:
    """A load adopts the dump's own type-check setting, and its suspension limit stands under the loader's as a
    ceiling: the count restarts and the re-announced call counts."""
    with pool.checkout(type_check=True) as a:
        run(t, lambda: a.feed_run('x: int = 1\nx'))
        checked = a.dump()
    with pool.checkout() as b:
        tried(t, lambda: b.load_session(checked))
        typed(t, lambda: b.feed_run('y: int = "a"\ny'))
    with pool.checkout() as c:
        plain = c.dump()
    with pool.checkout(type_check=True) as d:
        tried(t, lambda: d.load_session(plain))
        typed(t, lambda: d.feed_run('z: int = "a"\nz'))
    with pool.checkout(limits={'max_suspensions': 5}) as e:
        x = t.step(lambda: e.feed_start('f(1)\nf(2)\nf(3)\n"done"'))
        state = x.dump()
    for ceiling in (None, 0, 1, 2):
        kw = {} if ceiling is None else {'limits': {'max_suspensions': ceiling}}
        with pool.checkout(**kw) as g:
            r = tried(t, lambda: g.load_snapshot(state))
            announced(t, r)
            for n in range(3):
                if r is None or not hasattr(r, 'resume'):
                    break
                r = t.step(lambda: r.resume({'return_value': n}))
            run(t, lambda: g.feed_run('import time\ntime.sleep(0)\n7'))


# ------------------------------------------------------- host objects (C1-6)
def host_error_text(e: BaseException) -> str:
    if isinstance(e, pm.MontyError):
        return f'{type(e).__name__}: {e.display("type-msg")}'
    return f'{type(e).__name__}: {e}'


def host_probe(case_id: str, objects: Any, wrappers: Any, steps: list[Any]) -> Any:
    """One probe for a host_cases.py case: each step's outcome, as `host` events in order."""
    def body(pool: pm.Monty, t: T) -> None:
        objs, built = host_build(objects, wrappers)
        contexts: dict[str, Any] = {}
        sessions: dict[str, Any] = {}
        dumps: dict[str, bytes] = {}
        try:
            for step in steps:
                op, name, rest = step[0], step[1], step[2:]
                try:
                    if op == 'open':
                        contexts[name] = pool.checkout()
                        sessions[name] = contexts[name].__enter__()
                        outcome = 'ok'
                    elif op == 'feed':
                        value = sessions[name].feed_run(rest[0], inputs={n: built[n] for n in rest[1]} or None)
                        outcome = f'value: {host_show(value, objs)}'
                    elif op == 'dump':
                        dumps[name] = sessions[name].dump()
                        outcome = 'dumped'
                    elif op == 'load_session':
                        sessions[name].load_session(dumps[rest[0]])
                        outcome = 'ok'
                    else:
                        raise SystemExit(f'unknown host step {op}')
                except Exception as e:  # noqa: BLE001 - the outcome is the data
                    outcome = host_error_text(e)
                t.add({'ev': 'host', 'step': json.dumps(step, separators=(',', ':')), 'outcome': outcome})
        finally:
            for context in contexts.values():
                context.__exit__(None, None, None)

    body.__name__ = f'sp_host_{case_id}'
    return body


for _id, _objects, _wrappers, _steps in HOST_CASES:
    PROBES[f'sp_host_{_id}'] = host_probe(_id, _objects, _wrappers, _steps)


@probe
def host_wrapper_eq(pool: pm.Monty, t: T) -> None:
    """What upstream's wrappers do under `==` and `hash` (core's are identity, named divergence
    `host-wrapper-identity-eq` in EXCEPTIONS.md; no sandbox code can observe the difference, so this probe is in no
    gate): twins that carry one id are equal, a wrapper with another id is not, and neither wrapper is hashable."""
    objs, built = host_build({'o': [1, 2]}, [
        ['a', ['I', 'o', {'eager': None, 'lazy': None, 'methods': None}, {'id': 1}]],
        ['b', ['I', 'o', {'eager': None, 'lazy': None, 'methods': None}, {'id': 1}]],
        ['c', ['I', 'o', {'eager': None, 'lazy': None, 'methods': None}, {'id': 2}]],
        ['T', ['T', {'eager': None, 'lazy': None, 'methods': None}, {'id': 3}]],
    ])
    t.add({'ev': 'wrapper_eq', 'twins_equal': built['a'] == built['b'], 'other_id_equal': built['a'] == built['c'],
           'self_equal': built['a'] == built['a']})
    for name in ('a', 'T'):
        try:
            hash(built[name])
            t.add({'ev': 'wrapper_hash', 'wrapper': name, 'hashable': True})
        except TypeError:
            t.add({'ev': 'wrapper_hash', 'wrapper': name, 'hashable': False})


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


# ------------------------------------------------------- type checking (the M3 target)
def checked(t: T, fn: Any) -> None:
    """As `typed`, but a type-check rejection records its diagnostics, as the worker rendered them."""
    try:
        t.add({'ev': 'complete', 'value': canon(fn())})
    except pm.MontyTypingError as e:
        t.add({'ev': 'typing_error', 'display': e.display()})
    except Exception as e:  # noqa: BLE001 - recorded
        t.add(err_ev(e))


TC_STUBS = 'def f(x: int) -> int: ...\n'


@probe
def sp_type_check_stubs(pool: pm.Monty, t: T) -> None:
    """Stubs declare a host function to the checker: a call against the declaration is rejected (its diagnostic
    points into the stubs), a call that fits runs; without the stubs the name is unresolved."""
    ext = {'f': lambda x: x + 1}
    with pool.checkout(type_check=True, type_check_stubs=TC_STUBS) as s:
        checked(t, lambda: s.feed_run('f("a")', external_lookup=ext))
        checked(t, lambda: s.feed_run('f(2)', external_lookup=ext))
    with pool.checkout(type_check=True) as s:
        checked(t, lambda: s.feed_run('f(2)', external_lookup=ext))


TC_FORMATS = (None, 'full', 'concise', 'azure', 'json', 'jsonlines', 'rdjson', 'pylint', 'gitlab', 'github')


@probe
def sp_type_check_diagnostics(pool: pm.Monty, t: T) -> None:
    """One rejected snippet's diagnostics in every format, without and with colour (only `full` and `concise`
    colour); an unknown format is refused at checkout."""
    for fmt in TC_FORMATS:
        for color in (False, True):
            with pool.checkout(type_check=True, type_check_format=fmt, type_check_color=color) as s:
                t.add({'ev': 'format', 'format': fmt, 'color': color})
                checked(t, lambda: s.feed_run('x: int = "a"\nx'))
    # Only the argument check's refusal, with its text, counts: any other failure is recorded as the error it is.
    try:
        with pool.checkout(type_check=True, type_check_format='bogus'):  # type: ignore[arg-type]
            t.add({'ev': 'checkout'})
    except ValueError as e:
        t.add({'ev': 'format_refused', 'message': str(e)})
    except Exception as e:  # noqa: BLE001 - recorded
        t.add({'ev': 'unexpected', 'cls': type(e).__name__, 'msg': str(e)})


@probe
def sp_type_check_skip_feed(pool: pm.Monty, t: T) -> None:
    """A feed may skip the session's check: it runs, and its definitions do not join the checker's context; the
    next checked feed is checked again. Skipping on an unchecked session changes nothing; `feed_start` skips too."""
    with pool.checkout(type_check=True) as s:
        checked(t, lambda: s.feed_run('x: int = "a"\nx', skip_type_check=True))
        checked(t, lambda: s.feed_run('y: int = "b"\ny'))
        checked(t, lambda: s.feed_run('x + "!"'))
        x = t.step(lambda: s.feed_start('w: int = "d"\nf(w)', skip_type_check=True))
        if x is not None:
            t.step(lambda: x.resume({'return_value': 1}))
    with pool.checkout() as s:
        checked(t, lambda: s.feed_run('z: int = "c"\nz', skip_type_check=True))


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
