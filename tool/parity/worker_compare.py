"""Compares a worker-engine outcome with upstream's, field by field, as reference graphs.

The worker engine's comparison path (score.py --assert-e0 and --assert-worker). It differs
from the core engine's `norm` in four ways:

- **Shared objects up to relabelling.** A canonical value marks an object reached more than
  once with an `id` and writes later occurrences as `{"t": "ref", "id": n}`. Each side numbers
  its own references, and a set's members are sorted by their JSON text, so the same graph can
  be labelled differently on the two sides. Two values match when one bijection between the
  two sides' reference ids makes them equal, set and frozenset members matched as multisets.
  A shared object on one side and two equal copies on the other do not match.
- **Sandbox object ids up to relabelling.** Class and instance id fields (`instance_id`,
  `class_id`) match through one bijection per outcome, so equal ids stay equal and distinct ids
  stay distinct, across steps too.
- **Strings exactly.** No uuid or number inside a string is rewritten.
- **Host-side input refusals.** In a case marked `local_refusal`, a step whose upstream outcome
  is a host-side refusal (`HOST:<type>`: the host could not build the inputs, so nothing ran)
  matches a worker step the driver refused locally with `ArgumentError`, whatever the messages.

Floats compare by their bits, keys starting with `_` are display-only, and nothing else is
tolerated.
"""

from __future__ import annotations

import copy
import re
from typing import Any

ID_FIELDS = ('instance_id', 'class_id')
ALL_CODES = ['V', 'Et', 'Em', 'Ef', 'Ed', 'P', 'O', 'X', 'H', 'C']
FIELD = {'V': 'value', 'P': 'prints', 'O': 'os_calls', 'X': 'ext_calls', 'H': 'host_calls'}
EXC = {'Et': 'type', 'Em': 'message', 'Ef': 'traceback', 'Ed': 'data'}
CONFIG_KEYS = ('inputs', 'limits', 'os_policy', 'fs', 'cwd', 'mount_close')
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


class Budget(Exception):
    """The search took more steps than BUDGET: scored as a difference, never as a match."""


BUDGET = 1_000_000


class State:
    """Bijections built so far: reference ids (per root batch) and sandbox object ids (per outcome).

    Immutable: a binding returns a new state, so a search can try another pairing from an old one.
    """

    def __init__(self, ref_ab=None, ref_ba=None, id_ab=None, id_ba=None) -> None:
        self.ref_ab: dict = ref_ab or {}
        self.ref_ba: dict = ref_ba or {}
        self.id_ab: dict = id_ab or {}
        self.id_ba: dict = id_ba or {}

    def fresh_refs(self) -> State:
        return State({}, {}, self.id_ab, self.id_ba)

    def bind(self, refs: bool, a: Any, b: Any) -> tuple[State | None, bool]:
        """(state, newly bound); (None, False) when [a] or [b] is already bound elsewhere."""
        ab, ba = (self.ref_ab, self.ref_ba) if refs else (self.id_ab, self.id_ba)
        if a in ab or b in ba:
            return (self, False) if ab.get(a) == b and ba.get(b) == a else (None, False)
        ab, ba = {**ab, a: b}, {**ba, b: a}
        st = State(ab, ba, self.id_ab, self.id_ba) if refs else State(self.ref_ab, self.ref_ba, ab, ba)
        return st, True


def _defs(nodes: list[Any]) -> dict[Any, Any]:
    """Every node carrying an `id` in [nodes], by id (a reference graph's definitions)."""
    out: dict[Any, Any] = {}

    def walk(x: Any) -> None:
        if isinstance(x, list):
            for y in x:
                walk(y)
        elif isinstance(x, dict):
            if 'id' in x and x.get('t') != 'ref':
                out[x['id']] = x
            for k, y in x.items():
                if k != 'id':
                    walk(y)

    walk(nodes)
    return out


class Graphs:
    """One root batch on each side: its definitions, by reference id."""

    def __init__(self, a: list[Any], b: list[Any]) -> None:
        self.da, self.db = _defs(a), _defs(b)


def _step(g: Graphs, x: Any, y: Any, key: Any, st: State) -> tuple[State, list[tuple]] | None:
    """Matches [x] with [y] one level down: the new state and the pairs left to match, or None."""
    if isinstance(x, dict) and isinstance(y, dict) and ('id' in x or 'id' in y):
        if 'id' not in x or 'id' not in y:
            return None  # shared on one side only
        st2, new = st.bind(True, x['id'], y['id'])
        if st2 is None:
            return None
        if not new:
            return st2, []
        dx, dy = g.da.get(x['id']), g.db.get(y['id'])
        if dx is None or dy is None:
            return None  # a reference with no definition
        x, y, key, st = dx, dy, None, st2
    if isinstance(x, dict) and isinstance(y, dict):
        if x.get('t') != y.get('t'):
            return None
        if x.get('t') == 'float':
            return (st, []) if x.get('bits') == y.get('bits') else None
        keys = sorted(k for k in x if not k.startswith('_') and k != 'id')
        if keys != sorted(k for k in y if not k.startswith('_') and k != 'id'):
            return None
        return st, [('ms', g, x[k], y[k]) if k == 'items' and x.get('t') in ('set', 'frozenset')
                    else ('m', g, x[k], y[k], k) for k in keys]
    if isinstance(x, list) and isinstance(y, list):
        return (st, [('m', g, a, b, None) for a, b in zip(x, y)]) if len(x) == len(y) else None
    if key in ID_FIELDS and isinstance(x, str) and isinstance(y, str):
        st2, _ = st.bind(False, x, y)
        return None if st2 is None else (st2, [])
    return (st, []) if type(x) is type(y) and x == y else None


def _solve(tasks: list[tuple[str, list, list]], st: State) -> State | None:
    """A state under which every root batch matches, or None.

    A depth-first search with an explicit stack: the pairs left to match are a linked list, so a
    choice point (which member of a set pairs with which) saves them in O(1), and a later
    contradiction, in this batch or a later one, goes back to the last choice and tries the next.
    Set members are matched as multisets. Reference ids are bound afresh for each root batch;
    sandbox ids carry over between them.
    """
    work = None
    for _, a, b in reversed(tasks):
        work = (('task', a, b), work)
    stack = [(st, work)]
    steps = 0
    while stack:
        st, work = stack.pop()
        failed = False
        while work is not None:
            steps += 1
            if steps > BUDGET:
                raise Budget
            item, work = work
            if item[0] == 'task':
                g = Graphs(item[1], item[2])
                st = st.fresh_refs()
                work = (('m', g, item[1], item[2], None), work)
            elif item[0] == 'm':
                r = _step(item[1], item[2], item[3], item[4], st)
                if r is None:
                    failed = True
                    break
                st, children = r
                for c in reversed(children):
                    work = (c, work)
            else:  # 'ms': set members as a multiset
                _, g, xs, ys = item
                if len(xs) != len(ys):
                    failed = True
                    break
                if not xs:
                    continue
                alts = [(('m', g, xs[0], y, None), (('ms', g, xs[1:], ys[:j] + ys[j + 1:]), work))
                        for j, y in enumerate(ys)]
                for alt in reversed(alts[1:]):
                    stack.append((st, alt))
                work = alts[0]
        if not failed:
            return st
    return None


def _graph_tasks(rv: Any, gv: Any, code: str) -> list[tuple[list, list]] | None:
    """The root batches a field holds on each side; None when its shape already differs."""
    if code == 'V':
        return [([rv], [gv])]
    rv, gv = rv or [], gv or []
    if not isinstance(rv, list) or not isinstance(gv, list) or len(rv) != len(gv):
        return None
    tasks = []
    for ca, cb in zip(rv, gv):
        keys = sorted(ca.get('kwargs', {}))
        if ca.get('name') != cb.get('name') or keys != sorted(cb.get('kwargs', {})):
            return None
        tasks.append(([*ca['args'], *(ca['kwargs'][k] for k in keys)],
                      [*cb['args'], *(cb['kwargs'][k] for k in keys)]))
    return tasks


def _host_refusal(ref_step: dict) -> bool:
    return str((ref_step.get('exception') or {}).get('type', '')).startswith('HOST:')


def _local_refusal(got_step: dict) -> bool:
    return got_step.get('thrown') == 'ArgumentError'


def compare(ref: dict, got: dict, local_refusal: bool = False, codes: list[str] | None = None) -> list[str]:
    """Codes of the fields that differ between upstream's outcome [ref] and the worker's [got]."""
    codes = codes or ALL_CODES
    bad: list[str] = []
    rs, gs = ref.get('steps') or [], got.get('steps') or []
    if len(rs) != len(gs):
        return ['steps']
    if 'C' in codes and any(k.split('.')[0] in CONFIG_KEYS for k in got.get('unsupported') or {}):
        bad.append('C')
    tasks: list[tuple[str, list, list]] = []
    for n, (r, g) in enumerate(zip(rs, gs)):
        if local_refusal and _host_refusal(r):
            # Nothing ran upstream: the worker must have refused locally, and run nothing either.
            if not _local_refusal(g) or g.get('value') is not None or any(
                    g.get(f) for f in ('prints', 'os_calls', 'ext_calls', 'host_calls')):
                bad.append(f'refusal@{n}')
            continue
        if _local_refusal(g):
            bad.append(f'refusal@{n}')
            continue
        for code in codes:
            if code == 'C':
                continue
            if code in EXC:
                rv = (r.get('exception') or {}).get(EXC[code]) if r.get('exception') else r.get('exception')
                gv = (g.get('exception') or {}).get(EXC[code]) if g.get('exception') else g.get('exception')
                if rv != gv:
                    bad.append(code)
            elif code in ('V', 'O', 'X'):
                t = _graph_tasks(r.get(FIELD[code]), g.get(FIELD[code]), code)
                if t is None:
                    bad.append(code)
                else:
                    tasks += [(code, a, b) for a, b in t]
            elif r.get(FIELD[code]) != g.get(FIELD[code]):
                bad.append(code)
    try:
        if _solve(tasks, State()) is None:
            # Name the fields: each batch on its own, then (if each matches alone) the coupling.
            alone = [c for c, a, b in tasks if _solve([(c, a, b)], State()) is None]
            bad += alone or [tasks[-1][0]]
    except Budget:
        bad.append('budget')
    return sorted(set(bad), key=bad.index)


# ------------------------------------------------------------------ negative controls
def _nodes(x: Any, path: tuple = ()) -> list[tuple[tuple, Any]]:
    """Every (path, node) in [x], depth first, in document order."""
    out = [(path, x)]
    if isinstance(x, dict):
        for k, y in x.items():
            out += _nodes(y, path + (k,))
    elif isinstance(x, list):
        for i, y in enumerate(x):
            out += _nodes(y, path + (i,))
    return out


def _set(root: Any, path: tuple, value: Any) -> None:
    for p in path[:-1]:
        root = root[p]
    root[path[-1]] = value


def _values(got: dict) -> list[tuple[tuple, Any]]:
    """(path, node) over every step's value, in the outcome's own document."""
    return [((('steps', i, 'value') + p), n) for i, s in enumerate(got.get('steps') or [])
            for p, n in _nodes(s.get('value'))]


def _get(root: Any, path: tuple) -> Any:
    for p in path:
        root = root[p]
    return root


def _copy_ref(got: dict, want: Any) -> bool:
    """Replaces the first reference whose definition satisfies [want] by an unshared copy of it."""
    for i, s in enumerate(got.get('steps') or []):
        defs = _defs([s.get('value')])
        for p, n in _nodes(s.get('value')):
            if isinstance(n, dict) and n.get('t') == 'ref' and want(defs.get(n['id']), p, s.get('value')):
                dup = copy.deepcopy(defs[n['id']])
                del dup['id']
                _set(s, ('value',) + p, dup)
                return True
    return False


def _inside_proxy(path: tuple, root: Any) -> bool:
    return any(isinstance(_get(root, path[:k]), dict) and _get(root, path[:k]).get('t') == 'proxy'
               for k in range(len(path)))


def mutate(got: dict, mutation: str) -> bool:
    """Applies the named negative control to [got] in place; False when it has nothing to act on."""
    if mutation == 'copy-shared-root':
        return _copy_ref(got, lambda d, p, r: d is not None)
    if mutation == 'copy-shared-namedtuple':
        return _copy_ref(got, lambda d, p, r: d is not None and d.get('t') == 'namedtuple')
    if mutation == 'copy-shared-proxy':
        return _copy_ref(got, lambda d, p, r: d is not None and d.get('t') == 'proxy')
    if mutation == 'copy-shared-attribute-child':
        return _copy_ref(got, lambda d, p, r: d is not None and _inside_proxy(p, r))
    if mutation == 'copy-shared-call-arg':
        for s in got.get('steps') or []:
            for call in s.get('ext_calls') or []:
                batch = [*call['args'], *call['kwargs'].values()]
                defs = _defs(batch)
                for p, n in _nodes(batch):
                    if isinstance(n, dict) and n.get('t') == 'ref' and n['id'] in defs:
                        dup = copy.deepcopy(defs[n['id']])
                        del dup['id']
                        n.clear()
                        n.update(dup)
                        return True
        return False
    if mutation == 'retarget-ref':
        # The last reference in document order (outside any set in the controls' cases) now names
        # another shared object.
        for s in reversed(got.get('steps') or []):
            labels = sorted(_defs([s.get('value')]))
            for p, n in reversed(_nodes(s.get('value'))):
                if isinstance(n, dict) and n.get('t') == 'ref' and len(labels) > 1:
                    n['id'] = next(x for x in labels if x != n['id'])
                    return True
        return False
    if mutation in ('string-digits', 'string-uuid'):
        for p, n in _values(got):
            if isinstance(n, dict) and n.get('t') == 'str':
                v = n['v']
                m = UUID.search(v) if mutation == 'string-uuid' else re.search(r'\d', v)
                if m:
                    i = m.start()
                    n['v'] = v[:i] + ('1' if v[i] != '1' else '2') + v[i + 1:]
                    return True
        return False
    if mutation in ('merge-instance-ids', 'change-instance-id'):
        ids = [(p, n) for p, n in _values(got) if isinstance(n, dict) and 'instance_id' in n]
        if mutation == 'merge-instance-ids':
            distinct = [n for _, n in ids if n['instance_id'] != ids[0][1]['instance_id']] if ids else []
            for n in distinct:
                n['instance_id'] = ids[0][1]['instance_id']
            return bool(distinct)
        # An id seen more than once (the same object reached twice): one occurrence now differs.
        seen = [n['instance_id'] for _, n in ids]
        for _, n in reversed(ids):
            if seen.count(n['instance_id']) > 1:
                n['instance_id'] = '00000000-0000-4000-8000-000000000000'
                return True
        return False
    if mutation in ('drop-zone-name', 'empty-zone-name-to-none'):
        for p, n in _values(got):
            if isinstance(n, dict) and n.get('t') in ('timezone', 'time', 'datetime'):
                k = 'name' if n['t'] == 'timezone' else 'tzname'
                if n.get(k) is not None and (mutation == 'drop-zone-name' or n[k] == ''):
                    n[k] = None
                    return True
        return False
    raise ValueError(f'unknown negative control {mutation!r}')
