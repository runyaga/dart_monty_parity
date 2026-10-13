#!/usr/bin/env python3
"""Check that every third-party GitHub Action is pinned to a full commit SHA.

A tag such as `@v7` or a branch such as `@stable` is a pointer the action's
owner can move; the next workflow run executes whatever it points at, with
this repository's token. A 40-hex commit SHA cannot be moved.

Accepted:  uses: owner/repo[/path]@<40 lowercase hex>  (a `# v7` comment after
           it names the release it came from)
           uses: ./local/path                         (this repo's own code)
Rejected:  everything else, including tags, branches, short SHAs and docker://.

Scans .github/workflows/*.yml|*.yaml and every action.yml|action.yaml under
.github/actions/. Before the real tree it checks itself against two fixtures
(a tag pin must fail, a SHA pin must pass), and it fails when the tree yields
no `uses:` at all, so a pattern that stops matching is a failure, not a pass.
Dependabot (.github/dependabot.yml, ecosystem github-actions) proposes bumps.

usage: python3 -I tool/check_action_pins.py
Exit 0 on pass, 1 on any unpinned `uses:`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The key in any YAML spelling -- `uses:`, `uses :`, `"uses":`, `{uses: ...}` --
# anywhere on a line. A value the checker cannot read as a plain scalar (an
# alias, a block scalar) is rejected rather than skipped.
USES = re.compile(r'(?<![\w-])["\']?uses["\']?\s*:\s*(?P<v>[^\s,}]*)')
# Only whole-line comments are skipped: a `#` inside a quoted value such as
# `{name: "a # b", uses: x@v1}` is not a comment, so the rest of the line is
# still read (a `uses:` written in a trailing comment is reported, loudly).
COMMENT = re.compile(r'^\s*#')
PINNED = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[^@\s]+)?@[0-9a-f]{40}$')


def unpinned(name: str, text: str) -> tuple[int, list[str]]:
    """Return (number of `uses:` seen, offending `name:line: value` entries)."""
    seen, bad = 0, []
    for n, line in enumerate(text.splitlines(), 1):
        if COMMENT.match(line):
            continue
        for m in USES.finditer(line):
            seen += 1
            v = m.group('v').strip('"\'')
            if v.startswith('./') or PINNED.match(v):
                continue
            bad.append(f'{name}:{n}: {v}')
    return seen, bad


def files(root: Path) -> list[Path]:
    wf = root / '.github/workflows'
    found = [p for p in wf.glob('*') if p.suffix in ('.yml', '.yaml')]
    found += [p for p in (root / '.github/actions').rglob('action.y*ml')
              if p.name in ('action.yml', 'action.yaml')]
    return sorted(found)


def main() -> int:
    sha = ('      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7\n'
           '      - uses: ./.github/actions/x\n')
    for tag in ('      - uses: actions/checkout@v7',
                '      - uses : actions/checkout@v7',
                '      - "uses": actions/checkout@v7',
                '      - {uses: actions/checkout@v7, with: {fetch-depth: 0}}',
                '      - {name: "a # b", uses: actions/checkout@v7}'):
        if unpinned('fixture', tag) != (1, ['fixture:1: actions/checkout@v7']):
            print(f'FAIL: self-test -- a tag pin was not rejected: {tag.strip()}')
            return 1
    if unpinned('fixture', sha) != (2, []):
        print('FAIL: self-test -- a SHA pin was rejected.')
        return 1

    seen, bad = 0, []
    for p in files(ROOT):
        s, b = unpinned(str(p.relative_to(ROOT)), p.read_text())
        seen += s
        bad += b
    if seen == 0:
        print("FAIL: found no 'uses:' under .github -- the scan is not looking where the workflows are.")
        return 1
    if bad:
        print(f"FAIL: {len(bad)} of {seen} 'uses:' are not pinned to a full commit SHA:")
        for b in bad:
            print(f'    {b}')
        print('  Pin each to the 40-hex commit its tag resolves to, keeping the tag as a comment:')
        print('    uses: owner/repo@<sha> # v1')
        print('  (gh api repos/<owner>/<repo>/commits/<tag> --jq .sha)')
        return 1
    print(f"PASS -- all {seen} 'uses:' are SHA-pinned or local.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
