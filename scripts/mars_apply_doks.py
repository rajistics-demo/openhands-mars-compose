#!/usr/bin/env python3
"""Apply the DOKS/Helm MARS component to a released 1.97.1 base tree.

Usage: mars_apply_doks.py <shared-root> <doks-component-dir> <target-root> [--check]

This composes two sources instead of vendoring one tree:

- <shared-root> (normally this package's own ``enterprise/`` directory) -
  the provider-agnostic MARS code: everything under
  ``enterprise/overlay/openhands/`` *except* ``migrations/versions``, which
  is release-specific and never shared. This is the same code the 1.88.0
  Compose package applies via ``mars_apply.py enterprise <target>``; DOKS
  reuses it byte-for-byte rather than keeping a second copy.
- <doks-component-dir> (normally ``doks/enterprise``) - only what is
  genuinely different for a 1.97.1 Helm install: ``BASE_SHA256`` (hashes of
  the 1.97.1 stock files), ``patches/*.patch`` (the same hunks as the 1.88.0
  package's ``enterprise/patches``, re-anchored to 1.97.1 line numbers - see
  ``doks/VERSIONS.md``), ``overlay-migrations/*.py`` (the do_mars_doks_001/
  do_mars_doks_002 migration pair rebased onto release head 221 instead of
  213), and ``MIGRATION_HEAD``.

Fail-closed, like ``mars_apply.py``: nothing is written unless every stock
base file matches byte-for-byte, the release's migration head matches
MIGRATION_HEAD, every hunk applies at its exact position, no shared or
DOKS-owned file already exists in the target, and the complete in-memory
result (shared files + patched files + DOKS migrations) matches
PATCHED_SHA256. Only the Python standard library is used.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mars_apply import (
    ApplyError,
    apply_hunks,
    check_migration_head,
    parse_patch,
    read_manifest,
    sha256,
)

_EXCLUDED_SHARED_SUBTREE = ('migrations', 'versions')


def _shared_overlay_files(shared_root: Path) -> list[Path]:
    overlay = shared_root / 'overlay'
    files = []
    for path in sorted(p for p in overlay.rglob('*') if p.is_file()):
        rel_parts = path.relative_to(overlay).parts
        if '__pycache__' in rel_parts:
            continue
        if rel_parts[: len(_EXCLUDED_SHARED_SUBTREE)] == _EXCLUDED_SHARED_SUBTREE:
            continue
        files.append(path)
    return files


def plan(shared_root: Path, component: Path, root: Path) -> dict[Path, bytes]:
    check_migration_head(component, root)
    base = read_manifest(component / 'BASE_SHA256')
    expected = read_manifest(component / 'PATCHED_SHA256')

    for name, digest in base.items():
        path = root / name
        if not path.is_file() or sha256(path.read_bytes()) != digest:
            raise ApplyError(f'{name} is not the supported 1.97.1 release version')

    writes: dict[Path, bytes] = {}

    for patch in sorted((component / 'patches').glob('*.patch')):
        target, hunks = parse_patch(patch.read_text())
        if target not in base:
            raise ApplyError(f'{patch.name} targets unverified file {target}')
        current = writes.get(root / target, (root / target).read_bytes())
        writes[root / target] = apply_hunks(current.decode(), hunks).encode()

    for src in _shared_overlay_files(shared_root):
        rel = src.relative_to(shared_root / 'overlay')
        dest = root / rel
        if dest.exists():
            raise ApplyError(f'shared overlay file already exists: {rel}')
        writes[dest] = src.read_bytes()

    migrations_dir = component / 'overlay-migrations'
    if migrations_dir.is_dir():
        for src in sorted(migrations_dir.glob('*.py')):
            dest = root / 'migrations' / 'versions' / src.name
            if dest.exists():
                raise ApplyError(f'DOKS migration file already exists: {src.name}')
            writes[dest] = src.read_bytes()

    produced = {str(p.relative_to(root)): sha256(d) for p, d in writes.items()}
    if produced != expected:
        differing = sorted(
            k for k in set(expected) | set(produced) if produced.get(k) != expected.get(k)
        )
        raise ApplyError(f'result does not match PATCHED_SHA256: {differing}')
    return writes


def main(argv: list[str]) -> int:
    if len(argv) not in (4, 5) or (len(argv) == 5 and argv[4] != '--check'):
        print(__doc__, file=sys.stderr)
        return 2
    shared_root = Path(argv[1]).resolve()
    component = Path(argv[2]).resolve()
    root = Path(argv[3]).resolve()
    try:
        writes = plan(shared_root, component, root)
    except (ApplyError, OSError, UnicodeDecodeError, ValueError) as exc:
        print(f'[mars-doks] refusing: {exc}', file=sys.stderr)
        return 1
    if len(argv) == 5:
        print(f'[mars-doks] check ok: {len(writes)} files would change under {root}')
        return 0
    import shutil

    for path, data in writes.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + '.mars-tmp')
        tmp.write_bytes(data)
        if path.exists():
            shutil.copymode(path, tmp)
        tmp.replace(path)
    print(f'[mars-doks] applied {len(writes)} files under {root}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
