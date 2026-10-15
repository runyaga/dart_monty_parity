"""The host-object cases of the session probes (`sp_host_*`): one host class, its wrappers and steps.

Shared by run_probes.py (the oracle: upstream's `ClassInstance` / `ClassType` on pydantic-monty 1.1.0) and, through
`python3 -I tool/parity/oracle/host_cases.py --write`, by the Dart runner
(packages/monty_parity/lib/src/host_cases.g.dart, `--check` fails if it is stale). The case language is
core's own host oracle's (`tool/generate_host_oracle.py` there); the cases here are the same, minus the steps
that await an async host method (upstream's synchronous binding has no way to drive one; core's own oracle runs
them on the async binding).

A case is (id, objects, wrappers, steps):
- objects: name -> [x, y], a Point;
- wrappers, built in order: [name, spec], where spec is ['I', object, policies, options] (a ClassInstance;
  options: 'id' an int for uuid.UUID(int=n), 'class' a wrapper name for class_type) or ['T', policies, options]
  (a ClassType of Point; options: 'id', 'init', 'instance' the instance policies, 'convert': 'redact' to send
  every attribute 'x' as '***');
- policies: {'eager': .., 'lazy': .., 'methods': ..}, each None, 'all' or a list of names;
- steps: ['open', s]; ['feed', s, code, [input names]] (each input bound to the wrapper of that name);
  ['dump', s] (kept under s); ['load_session', s, src]; ['close', s].
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pydantic_monty as pm

HERE = Path(__file__).resolve().parent
DART = HERE.parent.parent.parent / 'packages/monty_parity/lib/src/host_cases.g.dart'


@dataclass
class Point:
    """The host class every case uses; _host_cases.dart mirrors it."""

    x: int
    y: int

    ORIGIN_NAME = 'origin'

    def sum(self) -> int:
        return self.x + self.y

    def shifted(self, dx: int, *, dy: int = 0) -> list[int]:
        return [self.x + dx, self.y + dy]

    async def later(self) -> int:
        return self.x * 10

    def fails(self) -> int:
        raise ValueError('no sum today')

    @property
    def norm1(self) -> int:
        return abs(self.x) + abs(self.y)

    @property
    def boom(self) -> int:
        raise KeyError('boom')

    @property
    def missing(self) -> int:
        raise AttributeError('missing')

    @classmethod
    def describe(cls) -> str:
        return 'Point class'

    @staticmethod
    def add(a: int, b: int) -> int:
        return a + b


class Redact(pm.ClassType):
    """A ClassType whose hook sends every attribute 'x' as '***'."""

    def convert_value(self, name: str, value: Any) -> Any:
        return '***' if name == 'x' else value


def P(eager=None, lazy=None, methods=None) -> dict[str, Any]:
    return {'eager': eager, 'lazy': lazy, 'methods': methods}


def feed(s: str, code: str, inputs: list[str] | None = None) -> list[Any]:
    return ['feed', s, code, inputs or []]


ALL = P('all', 'all', 'all')

# (id, objects, wrappers, steps).
CASES: list[tuple[str, dict[str, Any], list[Any], list[list[Any]]]] = [
    ('eager_all', {'o': [1, 2]}, [['p', ['I', 'o', P(eager='all'), {}]]], [
        ['open', 's'],
        feed('s', '(p.x, p.y)', ['p']),
        feed('s', 'p.norm1'),
    ]),
    ('eager_named', {'o': [1, 2]}, [['p', ['I', 'o', P(eager=['y', 'norm1']), {}]]], [
        ['open', 's'],
        feed('s', '(p.y, p.norm1)', ['p']),
        feed('s', 'p.x'),
    ]),
    ('eager_unknown_name', {'o': [1, 2]}, [['p', ['I', 'o', P(eager=['nope']), {}]]], [
        ['open', 's'],
        feed('s', '1', ['p']),
    ]),
    ('lazy_named', {'o': [1, -2]}, [['p', ['I', 'o', P(lazy=['norm1', 'x']), {}]]], [
        ['open', 's'],
        feed('s', '(p.norm1, p.x)', ['p']),
        feed('s', 'p.y'),
        feed('s', "getattr(p, 'y', 'dflt')"),
    ]),
    ('lazy_denied', {'o': [1, 2]}, [['p', ['I', 'o', P(), {}]]], [
        ['open', 's'],
        feed('s', 'p.norm1', ['p']),
        feed('s', "hasattr(p, 'norm1')"),
    ]),
    ('lazy_raises', {'o': [1, 2]}, [['p', ['I', 'o', P(lazy='all'), {}]]], [
        ['open', 's'],
        feed('s', 'p.boom', ['p']),
        feed('s', "getattr(p, 'boom', 5)"),
        feed('s', "try:\n    p.boom\nexcept KeyError as e:\n    r = repr(e)\nr"),
        feed('s', "hasattr(p, 'missing')"),
        feed('s', 'p.missing'),
        feed('s', "getattr(p, '_hidden', 'dflt')"),
    ]),
    ('methods', {'o': [3, 4]}, [['p', ['I', 'o', P(methods=['sum', 'shifted', 'fails']), {}]]], [
        ['open', 's'],
        feed('s', 'p.sum()', ['p']),
        feed('s', 'p.shifted(1, dy=2)'),
        feed('s', 'p.fails()'),
        feed('s', 'p.later()'),
        feed('s', 'p.nope()'),
    ]),
    ('methods_all', {'o': [3, 4]}, [['p', ['I', 'o', P(methods='all'), {}]]], [
        ['open', 's'],
        feed('s', 'p.sum()', ['p']),
        feed('s', 'p()'),
        feed('s', 'p.describe()'),
    ]),
    ('methods_denied', {'o': [3, 4]}, [['p', ['I', 'o', P(), {}]]], [
        ['open', 's'],
        feed('s', 'p.sum()', ['p']),
    ]),
    ('class_methods', {}, [['T', ['T', P(eager='all', methods='all'), {}]]], [
        ['open', 's'],
        feed('s', '(T.describe(), T.add(2, 3), T.ORIGIN_NAME)', ['T']),
        feed('s', 'T.sum()'),
        feed('s', 'T(1, 2)'),
        feed('s', 'T.nope'),
    ]),
    ('class_construct', {}, [['T', ['T', P(), {'init': True, 'instance': P(eager='all', methods=['sum'])}]]], [
        ['open', 's'],
        feed('s', 'q = T(3, 4)\n(q.x, q.y, q.sum(), type(q) is T)', ['T']),
        feed('s', 'q.norm1'),
        feed('s', 'T(1)'),
    ]),
    ('type_identity', {'o': [1, 2]}, [
        ['T', ['T', P(), {'init': True}]],
        ['p', ['I', 'o', P(eager='all'), {'class': 'T'}]],
    ], [
        ['open', 's'],
        feed('s', '(type(p) is T, type(p).__name__)', ['p', 'T']),
    ]),
    ('class_grant_kept', {'o': [1, 2]}, [
        ['T', ['T', P(), {'init': True, 'instance': P(eager='all')}]],
        ['p', ['I', 'o', P(eager='all'), {'class': 'U'}]],
    ], [
        ['open', 's'],
        feed('s', 'T(5, 6).x', ['T']),
        feed('s', 'p.x', ['p']),
        feed('s', 'T(7, 8).y'),
    ]),
    ('convert_hook', {'o': [1, 2]}, [
        ['T', ['T', P(eager='all'), {'convert': 'redact'}]],
        ['p', ['I', 'o', P(eager=['x', 'y'], lazy=['norm1']), {'class': 'T'}]],
    ], [
        ['open', 's'],
        feed('s', '(p.x, p.y, p.norm1)', ['p']),
    ]),
    ('identity', {'o': [1, 2], 'o2': [5, 6]}, [
        ['p', ['I', 'o', P(eager='all'), {}]],
        ['q', ['I', 'o2', P(), {}]],
    ], [
        ['open', 's'],
        feed('s', 'p', ['p']),
        feed('s', '[p, (p, p)]'),
        feed('s', '(p is p, q)', ['q']),
        feed('s', 'class C:\n    pass\nC()'),
    ]),
    ('rewrap_replaces_policy', {'o': [1, 2]}, [
        ['p', ['I', 'o', P(), {'id': 1}]],
        ['p2', ['I', 'o', P(methods=['sum']), {'id': 1}]],
    ], [
        ['open', 's'],
        feed('s', 'p.sum()', ['p']),
        feed('s', '1', ['p2']),
        feed('s', 'p.sum()'),
    ]),
    ('alias_refused', {'o': [1, 2], 'o2': [3, 4]}, [
        ['p', ['I', 'o', P(eager='all'), {'id': 2}]],
        ['q', ['I', 'o2', P(eager='all'), {'id': 2}]],
    ], [
        ['open', 's'],
        feed('s', 'p.x', ['p']),
        feed('s', 'q.x', ['q']),
        feed('s', 'p.x'),
    ]),
    ('restore_store_empty', {'o': [1, 2]}, [
        ['p', ['I', 'o', P(eager='all', lazy=['norm1'], methods=['sum']), {'id': 3}]],
    ], [
        ['open', 's'],
        feed('s', 'p.sum()', ['p']),
        ['dump', 's'],
        ['open', 't'],
        ['load_session', 't', 's'],
        feed('t', 'p.x'),
        feed('t', 'p.sum()'),
        feed('t', 'p.norm1'),
        feed('t', 'p'),
        feed('s', 'p'),
    ]),
]


def policies(spec: dict[str, Any], prefix: str = '') -> dict[str, Any]:
    return {
        f'{prefix}eager_attrs': spec['eager'],
        f'{prefix}lazy_attrs': spec['lazy'],
        f'{prefix}allowed_methods': spec['methods'],
    }


def build(objects: dict[str, Any], wrappers: list[Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    objs = {name: Point(*xy) for name, xy in objects.items()}
    built: dict[str, Any] = {}
    for name, spec in wrappers:
        opts = spec[-1]
        ident = uuid.UUID(int=opts['id']) if 'id' in opts else None
        if spec[0] == 'T':
            cls = Redact if opts.get('convert') == 'redact' else pm.ClassType
            built[name] = cls(
                Point,
                id=ident,
                init=opts.get('init', False),
                **policies(spec[1]),
                **policies(opts.get('instance', P()), 'instance_'),
            )
        else:
            class_type = built.get(opts['class']) if 'class' in opts else None
            if 'class' in opts and class_type is None:
                class_type = pm.ClassType(Point)
            built[name] = pm.ClassInstance(
                objs[spec[1]],
                **({'id': ident} if ident else {}),
                class_type=class_type,
                **policies(spec[2]),
            )
    return objs, built


def show(v: Any, objs: dict[str, Any]) -> str:
    for name, obj in objs.items():
        if v is obj:
            return f'is:{name}'
    if isinstance(v, pm.MontyClassProxy):
        return f'proxy:{v.name}'
    if isinstance(v, list):
        return '[' + ', '.join(show(x, objs) for x in v) + ']'
    if isinstance(v, tuple):
        inner = ', '.join(show(x, objs) for x in v)
        return f'({inner},)' if len(v) == 1 else f'({inner})'
    return repr(v)


def show(v: Any, objs: dict[str, Any]) -> str:
    """A value as the probes record it: a host object is `is:<name>`, a proxy `proxy:<class>`, else its repr."""
    for name, obj in objs.items():
        if v is obj:
            return f'is:{name}'
    if isinstance(v, pm.MontyClassProxy):
        return f'proxy:{v.name}'
    if isinstance(v, list):
        return '[' + ', '.join(show(x, objs) for x in v) + ']'
    if isinstance(v, tuple):
        inner = ', '.join(show(x, objs) for x in v)
        return f'({inner},)' if len(v) == 1 else f'({inner})'
    return repr(v)


def dart_source() -> str:
    cases = [{'id': c, 'objects': o, 'wrappers': w, 'steps': s} for c, o, w, s in CASES]
    text = json.dumps(cases, ensure_ascii=True, indent=1)
    return ("// The host-object probe cases of tool/parity/oracle/host_cases.py, as JSON.\n"
            "// Generated by `python3 -I tool/parity/oracle/host_cases.py --write`; do not edit by hand.\n\n"
            "/// The cases, as JSON.\n"
            "const hostCasesJson = r\'\'\'\n" + text + "\n\'\'\';\n")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ''
    if mode == '--write':
        DART.write_text(dart_source())
        return 0
    if mode == '--check':
        if not DART.exists() or DART.read_text() != dart_source():
            print(f'{DART.name} differs from host_cases.py', file=sys.stderr)
            return 1
        return 0
    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main())
