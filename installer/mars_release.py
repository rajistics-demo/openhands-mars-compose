#!/usr/bin/env python3
"""Deterministic release ZIP + checksums for the MARS evaluation package (stdlib only).

  python3 installer/mars_release.py sums --check|--write
  python3 installer/mars_release.py build --out DIR [--source-commit SHA]
  python3 installer/mars_release.py verify ZIP

Only files in the committed allowlist installer/release-files.txt are shipped.
In a git checkout every listed file must be tracked, unmodified and have the
listed mode; the HEAD commit is recorded as verified provenance. Outside git
(e.g. an extracted bundle) --source-commit is required and is recorded as
declared, not verified. SHA256 values detect corruption only; they are not
signatures and prove nothing about who produced the archive.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = 'installer/release-files.txt'
LOCK = 'installer/release.lock.json'
GENERATED = ('RELEASE.json', 'SHA256SUMS')
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
NAME_PREFIX = 'openhands-enterprise-mars-'
VERSION_RE = re.compile(r'^1\.88\.0-mars[1-9][0-9]{0,3}$')
COMMIT_RE = re.compile(r'^[0-9a-f]{40}$')
COMPONENT_RE = re.compile(r'^[A-Za-z0-9._-]+$')
DENY_COMPONENTS = {
    '.git', '__pycache__', '.pytest_cache', '.ruff_cache', '.mypy_cache', '.venv', 'venv', 'node_modules',
    'secrets', '.private', 'private', 'artifacts', 'logs', '.pr',
}
DENY_SUFFIXES = ('.pyc', '.pyo', '.zip', '.tar', '.gz', '.tgz', '.log', '.pem', '.key', '.p12', '.sqlite', '.db', '.sha256')
DENY_NAMES = {'.env', 'state.json', 'stack.env', 'compose.env', 'environment.yaml', 'SHA256SUMS', 'RELEASE.json'}


class ReleaseError(Exception):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_path(rel: str) -> None:
    parts = rel.split('/')
    if not rel or rel.startswith('/') or '\\' in rel or any(p in ('', '.', '..') for p in parts):
        raise ReleaseError(f'unsafe path in manifest: {rel!r}')
    if not all(COMPONENT_RE.match(p) for p in parts):
        raise ReleaseError(f'unsupported characters in manifest path: {rel!r}')
    name = parts[-1]
    if (DENY_COMPONENTS & set(parts) or name in DENY_NAMES or name.endswith(DENY_SUFFIXES)
            or (name.endswith('.env') and not name.endswith('.env.example'))):
        raise ReleaseError(f'manifest path is on the deny list (cache, secret, env, log or archive): {rel}')


def read_manifest(root: Path) -> list[tuple[str, int]]:
    path = root / MANIFEST
    if path.is_symlink() or not path.is_file():
        raise ReleaseError(f'{MANIFEST} is missing')
    entries: list[tuple[str, int]] = []
    for n, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        if not line or line.startswith('#'):
            continue
        m = re.match(r'^(0644|0755) (\S+)$', line)
        if not m:
            raise ReleaseError(f'{MANIFEST} line {n}: expected "<0644|0755> <path>"')
        check_path(m.group(2))
        entries.append((m.group(2), int(m.group(1), 8)))
    names = [e[0] for e in entries]
    if names != sorted(names) or len(set(names)) != len(names):
        raise ReleaseError(f'{MANIFEST} must be sorted and free of duplicates')
    for required in (MANIFEST, LOCK, 'installer/mars_setup.py', 'installer/mars_release.py'):
        if required not in names:
            raise ReleaseError(f'{MANIFEST} must list {required}')
    return entries


def load_version(root: Path) -> str:
    try:
        version = json.loads((root / LOCK).read_text(encoding='utf-8'))['package_version']
    except (OSError, ValueError, KeyError):
        raise ReleaseError('cannot read package_version from the release lock') from None
    if not isinstance(version, str) or not VERSION_RE.match(version):
        raise ReleaseError('package_version is not a supported 1.88.0-marsN version')
    return version


def git(root: Path, *args: str) -> str:
    res = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, check=False,
                         stdin=subprocess.DEVNULL)
    if res.returncode != 0:
        raise ReleaseError(f'git {args[0]} failed')
    return res.stdout


def collect(root: Path, declared_commit: str | None) -> tuple[list[tuple[str, int, bytes]], dict]:
    entries = read_manifest(root)
    files = []
    for rel, mode in entries:
        path = root / rel
        cur = root
        for part in rel.split('/'):
            cur = cur / part
            if cur.is_symlink():
                raise ReleaseError(f'symlink in release path: {rel}')
        if not path.is_file():
            raise ReleaseError(f'listed file is missing: {rel}')
        files.append((rel, mode, path.read_bytes()))

    if (root / '.git').exists():
        listed = {rel for rel, _ in entries}
        tracked = {}
        for line in git(root, 'ls-files', '-s', '-z').split('\0'):
            if line:
                meta, name = line.split('\t', 1)
                tracked[name] = meta.split()[0]
        for rel, mode in entries:
            if tracked.get(rel) is None:
                raise ReleaseError(f'listed file is not tracked by git: {rel}')
            if tracked[rel] != ('100755' if mode == 0o755 else '100644'):
                raise ReleaseError(f'git mode of {rel} differs from the manifest')
        dirty = git(root, 'status', '--porcelain', '--untracked-files=no', '--', *sorted(listed))
        if dirty.strip():
            raise ReleaseError('listed files have uncommitted changes; commit them first')
        commit = git(root, 'rev-parse', 'HEAD').strip()
        if declared_commit and declared_commit != commit:
            raise ReleaseError('--source-commit differs from git HEAD')
        provenance = {'source_commit': commit, 'source_commit_verified': True, 'source_kind': 'git'}
    else:
        if not declared_commit or not COMMIT_RE.match(declared_commit):
            raise ReleaseError('outside a git checkout --source-commit <40-hex commit> is required')
        for rel, mode, _ in files:
            exec_bit = bool((root / rel).stat().st_mode & stat.S_IXUSR)
            if exec_bit != (mode == 0o755):
                raise ReleaseError(f'file mode of {rel} differs from the manifest')
        provenance = {'source_commit': declared_commit, 'source_commit_verified': False,
                      'source_kind': 'declared (no git metadata)'}
    return files, provenance


def sums_text(files: list[tuple[str, int, bytes]]) -> str:
    return ''.join(f'{sha256(data)}  {rel}\n' for rel, _, data in sorted(files))


def zip_entry(name: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=ZIP_EPOCH)
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | mode) << 16
    info.compress_type = zipfile.ZIP_STORED
    return info


def build(root: Path, out_dir: Path, declared_commit: str | None) -> tuple[Path, str]:
    root = root.resolve()
    out_dir = out_dir.resolve()
    if out_dir == root or root in out_dir.parents:
        raise ReleaseError('--out must be outside the source tree')
    version = load_version(root)
    files, provenance = collect(root, declared_commit)
    top = f'{NAME_PREFIX}{version}'
    lock = json.loads((root / LOCK).read_text(encoding='utf-8'))
    release = {
        'package_version': version,
        'root': top,
        'enterprise_version': lock['enterprise']['version'],
        'enterprise_commit': lock['enterprise']['commit'],
        'stock_compose_sha256': lock['enterprise']['compose']['sha256'],
        **provenance,
        'manifest_sha256': sha256((root / MANIFEST).read_bytes()),
        'file_count': len(files),
        'integrity_note': 'SHA256 values detect corruption only; they are unsigned and are not an authenticity claim.',
    }
    files.append(('RELEASE.json', 0o644, (json.dumps(release, indent=2, sort_keys=True) + '\n').encode()))
    files.sort()
    all_files = files + [('SHA256SUMS', 0o644, sums_text(files).encode())]
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f'{top}.zip'
    sidecar = out_dir / f'{target.name}.sha256'
    if target.is_symlink() or sidecar.is_symlink():
        raise ReleaseError('release outputs must not be symlinks')
    fd, tmp = tempfile.mkstemp(prefix='.release.', suffix='.zip', dir=out_dir)
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, 'w') as zf:
            for rel, mode, data in sorted(all_files):
                zf.writestr(zip_entry(f'{top}/{rel}', mode), data)
        data = Path(tmp).read_bytes()
        digest = sha256(data)
        if target.exists() and sha256(target.read_bytes()) != digest:
            raise ReleaseError(f'{target.name} already exists with different content; refusing to overwrite')
        expected_sidecar = f'{digest}  {target.name}\n'.encode()
        if sidecar.exists():
            if sidecar.read_bytes() != expected_sidecar:
                raise ReleaseError('checksum sidecar exists with different content; refusing to overwrite')
        else:
            try:
                side_fd = os.open(sidecar, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            except FileExistsError:
                raise ReleaseError('checksum sidecar appeared during build; refusing to overwrite') from None
            with os.fdopen(side_fd, 'wb') as side:
                side.write(expected_sidecar)
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return target, digest


def verify(zip_path: Path) -> dict:
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        tops = {i.filename.split('/', 1)[0] for i in infos}
        if len(tops) != 1 or not next(iter(tops)).startswith(NAME_PREFIX):
            raise ReleaseError('archive must have exactly one versioned root directory')
        top = next(iter(tops))
        contents = {}
        for info in infos:
            rel = info.filename[len(top) + 1:]
            kind = stat.S_IFMT(info.external_attr >> 16)
            if info.is_dir() or kind != stat.S_IFREG:
                raise ReleaseError(f'non-regular entry in archive: {info.filename}')
            if rel not in GENERATED:
                check_path(rel)
            if rel in contents:
                raise ReleaseError(f'duplicate archive entry: {info.filename}')
            contents[rel] = zf.read(info)
    if 'SHA256SUMS' not in contents or 'RELEASE.json' not in contents:
        raise ReleaseError('archive lacks SHA256SUMS or RELEASE.json')
    listed = {}
    for line in contents['SHA256SUMS'].decode().splitlines():
        m = re.match(r'^([0-9a-f]{64})  (\S+)$', line)
        if not m or m.group(2) in listed:
            raise ReleaseError('SHA256SUMS is malformed')
        listed[m.group(2)] = m.group(1)
    expected = {rel: sha256(data) for rel, data in contents.items() if rel != 'SHA256SUMS'}
    if listed != expected:
        raise ReleaseError('SHA256SUMS does not match the archive contents exactly')
    release = json.loads(contents['RELEASE.json'])
    if release.get('root') != top or top != f'{NAME_PREFIX}{release.get("package_version")}':
        raise ReleaseError('RELEASE.json root/version does not match the archive root')
    return release


def cmd_sums(args) -> int:
    files, _ = collect_for_sums(ROOT)
    text = sums_text(files)
    current = (ROOT / 'SHA256SUMS').read_text(encoding='utf-8') if (ROOT / 'SHA256SUMS').exists() else ''
    if args.write:
        (ROOT / 'SHA256SUMS').write_text(text, encoding='utf-8')
        print(f'wrote SHA256SUMS ({len(files)} files)')
        return 0
    if current != text:
        print('SHA256SUMS does not match the release manifest files; run sums --write')
        return 1
    print(f'SHA256SUMS matches {len(files)} manifest files')
    return 0


def collect_for_sums(root: Path):
    entries = read_manifest(root)
    files = []
    for rel, mode in entries:
        path = root / rel
        if path.is_symlink() or not path.is_file():
            raise ReleaseError(f'listed file is missing or a symlink: {rel}')
        files.append((rel, mode, path.read_bytes()))
    return files, None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog='mars_release.py', description=__doc__.split('\n\n')[0])
    sub = p.add_subparsers(dest='command', required=True)
    sp = sub.add_parser('sums', help='check or rewrite the source SHA256SUMS from the manifest')
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument('--check', action='store_true')
    g.add_argument('--write', action='store_true')
    sp = sub.add_parser('build', help='write <out>/openhands-enterprise-mars-<version>.zip and .sha256')
    sp.add_argument('--out', required=True)
    sp.add_argument('--source-commit')
    sp = sub.add_parser('verify', help='verify a release ZIP structure and SHA256SUMS')
    sp.add_argument('zip')
    args = p.parse_args(argv)
    try:
        if args.command == 'sums':
            return cmd_sums(args)
        if args.command == 'build':
            target, digest = build(ROOT, Path(args.out), args.source_commit)
            print(f'{digest}  {target}')
            return 0
        release = verify(Path(args.zip))
        print(f'ok {release["root"]}: {release["file_count"]} files + RELEASE.json, source {release["source_commit"]} '
              f'({"verified from git" if release["source_commit_verified"] else "declared, not verified"})')
        return 0
    except (ReleaseError, zipfile.BadZipFile) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
