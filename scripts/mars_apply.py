#!/usr/bin/env python3
"""Apply a MARS overlay to a released base tree, fail-closed.

Usage: mars_apply.py <component-dir> <target-root> [--check]

<component-dir> holds BASE_SHA256, PATCHED_SHA256, patches/*.patch and an
optional overlay/ tree. Nothing is written unless every base file matches the
released version byte-for-byte, every hunk applies at its exact position,
no overlay file already exists and the in-memory result matches
PATCHED_SHA256. Only the Python standard library is used, so the stock image
needs no git or patch binary.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import sys
from pathlib import Path

REVISION = re.compile(r'^revision\s*(?::[^=]*)?=\s*[\'"]([^\'"]+)', re.M)
DOWN_REVISION = re.compile(r'^down_revision\s*(?::[^=]*)?=\s*[\'"]([^\'"]+)', re.M)
HUNK = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')


class ApplyError(Exception):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_manifest(path: Path) -> dict[str, str]:
    entries = {}
    for line in path.read_text().splitlines():
        if line.strip():
            digest, name = line.split(None, 1)
            entries[name.strip()] = digest
    return entries


def parse_patch(text: str) -> tuple[str, list[tuple[int, list[str]]]]:
    lines = text.splitlines(keepends=True)
    target = None
    hunks: list[tuple[int, list[str]]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('+++ '):
            name = line[4:].strip()
            if not name.startswith('b/'):
                raise ApplyError(f'unexpected patch target {name!r}')
            target = name[2:]
        match = HUNK.match(line)
        if match:
            old_count = int(match.group(2) or '1')
            new_count = int(match.group(4) or '1')
            body = []
            i += 1
            seen_old = seen_new = 0
            while i < len(lines) and (seen_old < old_count or seen_new < new_count):
                body_line = lines[i]
                if body_line.startswith('\\'):
                    i += 1
                    continue
                tag = body_line[:1]
                if tag == ' ':
                    seen_old += 1
                    seen_new += 1
                elif tag == '-':
                    seen_old += 1
                elif tag == '+':
                    seen_new += 1
                else:
                    raise ApplyError(f'malformed hunk line {body_line!r}')
                body.append(body_line)
                i += 1
            hunks.append((int(match.group(1)), body))
            continue
        i += 1
    if target is None or not hunks:
        raise ApplyError('patch has no target or hunks')
    return target, hunks


def apply_hunks(original: str, hunks: list[tuple[int, list[str]]]) -> str:
    src = original.splitlines(keepends=True)
    out: list[str] = []
    pos = 0
    for start, body in hunks:
        begin = max(start - 1, 0)
        if begin < pos:
            raise ApplyError('overlapping hunks')
        out.extend(src[pos:begin])
        pos = begin
        for body_line in body:
            tag, content = body_line[:1], body_line[1:]
            if tag in (' ', '-'):
                if pos >= len(src) or src[pos].rstrip('\n') != content.rstrip('\n'):
                    raise ApplyError(f'hunk does not match at line {pos + 1}')
                if tag == ' ':
                    out.append(src[pos])
                pos += 1
            else:
                out.append(content if content.endswith('\n') else content + '\n')
    out.extend(src[pos:])
    return ''.join(out)


def check_migration_head(component: Path, root: Path) -> None:
    """MIGRATION_HEAD names '<versions dir> <revision>': the release's only head."""
    spec = component / 'MIGRATION_HEAD'
    if not spec.is_file():
        return
    versions, expected = spec.read_text().split()
    revisions, parents = set(), set()
    for path in (root / versions).glob('*.py'):
        text = path.read_text()
        rev = REVISION.search(text)
        if rev:
            revisions.add(rev.group(1))
        down = DOWN_REVISION.search(text)
        if down:
            parents.add(down.group(1))
    heads = revisions - parents
    if heads != {expected}:
        raise ApplyError(
            f'migration head is {sorted(heads)}, expected only {expected!r}; '
            'this package is for that release only'
        )


def plan(component: Path, root: Path) -> dict[Path, bytes]:
    check_migration_head(component, root)
    base = read_manifest(component / 'BASE_SHA256')
    expected = read_manifest(component / 'PATCHED_SHA256')
    for name, digest in base.items():
        path = root / name
        if not path.is_file() or sha256(path.read_bytes()) != digest:
            raise ApplyError(f'{name} is not the supported release version')
    writes: dict[Path, bytes] = {}
    for patch in sorted((component / 'patches').glob('*.patch')):
        target, hunks = parse_patch(patch.read_text())
        if target not in base:
            raise ApplyError(f'{patch.name} targets unverified file {target}')
        current = writes.get(root / target, (root / target).read_bytes())
        writes[root / target] = apply_hunks(current.decode(), hunks).encode()
    overlay = component / 'overlay'
    if overlay.is_dir():
        for src in sorted(p for p in overlay.rglob('*') if p.is_file()):
            rel = src.relative_to(overlay)
            if '__pycache__' in rel.parts:
                continue
            if (root / rel).exists():
                raise ApplyError(f'overlay file already exists: {rel}')
            writes[root / rel] = src.read_bytes()
    produced = {str(p.relative_to(root)): sha256(d) for p, d in writes.items()}
    if produced != expected:
        differing = sorted(
            k for k in set(expected) | set(produced) if produced.get(k) != expected.get(k)
        )
        raise ApplyError(f'result does not match PATCHED_SHA256: {differing}')
    return writes


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4) or (len(argv) == 4 and argv[3] != '--check'):
        print(__doc__, file=sys.stderr)
        return 2
    component, root = Path(argv[1]).resolve(), Path(argv[2]).resolve()
    try:
        writes = plan(component, root)
    except (ApplyError, OSError, UnicodeDecodeError, ValueError) as exc:
        print(f'[mars] refusing: {exc}', file=sys.stderr)
        return 1
    if len(argv) == 4:
        print(f'[mars] check ok: {len(writes)} files would change under {root}')
        return 0
    for path, data in writes.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + '.mars-tmp')
        tmp.write_bytes(data)
        if path.exists():
            shutil.copymode(path, tmp)
        tmp.replace(path)
    print(f'[mars] applied {len(writes)} files under {root}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
