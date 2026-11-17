"""Stdlib tests for installer/mars_release.py (deterministic ZIP, allowlist, provenance)."""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parents[1]
sys.path.insert(0, str(PKG / 'installer'))
import mars_release as mr  # noqa: E402
import mars_setup as ms  # noqa: E402

COMMIT = 'f0f9fc7' + '0' * 33
HAVE_GIT = shutil.which('git') is not None


def copy_package(dest: Path) -> Path:
    shutil.copytree(PKG, dest, symlinks=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git'))
    return dest


class Release(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='mars-release-test-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = copy_package(self.tmp / 'src')
        self.out = self.tmp / 'out'

    def build(self, root=None, commit=COMMIT, out=None):
        return mr.build(root or self.src, out or self.out, commit)

    def names(self, zip_path):
        with zipfile.ZipFile(zip_path) as zf:
            return [i.filename for i in zf.infolist()]

    def test_deterministic_archive_root_sidecar_and_exec_bits(self):
        zip1, digest1 = self.build()
        data1 = zip1.read_bytes()
        zip2, digest2 = self.build(out=self.tmp / 'out2')
        self.assertEqual(data1, zip2.read_bytes())
        self.assertEqual(digest1, digest2)
        self.assertEqual(zip1.name, 'openhands-enterprise-mars-1.88.0-mars1.zip')
        self.assertEqual((self.out / f'{zip1.name}.sha256').read_text(),
                         f'{hashlib.sha256(data1).hexdigest()}  {zip1.name}\n')
        release = mr.verify(zip1)
        self.assertFalse(release['source_commit_verified'])
        self.assertIn('not an authenticity claim', release['integrity_note'])
        with zipfile.ZipFile(zip1) as zf:
            modes = {i.filename.split('/', 1)[1]: (i.external_attr >> 16) & 0o777 for i in zf.infolist()}
            self.assertTrue(all(i.date_time == (1980, 1, 1, 0, 0, 0) for i in zf.infolist()))
        for rel in ('installer/mars_setup.py', 'installer/mars_release.py', 'scripts/preflight.sh',
                    'scripts/mars_apply.py'):
            self.assertEqual(modes[rel], 0o755, rel)
        self.assertEqual(modes['README.md'], 0o644)
        manifest = {rel for rel, _ in mr.read_manifest(self.src)}
        self.assertEqual(set(modes), manifest | {'RELEASE.json', 'SHA256SUMS'})

    def test_sidecar_symlink_or_unknown_content_is_not_overwritten(self):
        self.out.mkdir()
        name = 'openhands-enterprise-mars-1.88.0-mars1.zip'
        sidecar = self.out / (name + '.sha256')
        victim = self.tmp / 'operator-file'
        victim.write_text('preserve this data')
        sidecar.symlink_to(victim)
        with self.assertRaises(mr.ReleaseError):
            self.build()
        self.assertEqual(victim.read_text(), 'preserve this data')
        self.assertFalse((self.out / name).exists())
        sidecar.unlink()
        sidecar.write_text('unknown prior checksum')
        with self.assertRaises(mr.ReleaseError):
            self.build()
        self.assertEqual(sidecar.read_text(), 'unknown prior checksum')
        self.assertFalse((self.out / name).exists())

    def test_untracked_secrets_caches_archives_and_logs_are_not_shipped(self):
        planted = ['compose/mars.env', '.env', 'secrets/do_team_token', 'scripts/__pycache__/x.pyc',
                   'old-release.zip', 'test-results/raw.log', 'private/stack.env', 'installer/state.json']
        marker = 'planted-' + os.urandom(8).hex()
        for rel in planted:
            (self.src / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.src / rel).write_text(marker + '\n')
        zip_path, _ = self.build()
        names = self.names(zip_path)
        encoded = marker.encode()
        for rel in planted:
            self.assertFalse(any(n.endswith('/' + rel) for n in names), rel)
        with zipfile.ZipFile(zip_path) as zf:
            self.assertFalse(any(encoded in zf.read(n) for n in names))

    def test_symlinks_fail_closed(self):
        target = self.src / 'README.md'
        target.rename(self.src / 'README.real')
        os.symlink(self.src / 'README.real', target)
        with self.assertRaises(mr.ReleaseError):
            self.build()
        target.unlink()
        (self.src / 'README.real').rename(target)
        shutil.move(self.src / 'evidence', self.tmp / 'evidence-real')
        os.symlink(self.tmp / 'evidence-real', self.src / 'evidence')
        with self.assertRaises(mr.ReleaseError):
            self.build()

    def test_manifest_validation_fails_closed(self):
        manifest = self.src / mr.MANIFEST
        good = manifest.read_text()
        for bad_line in ('0644 ../outside.txt', '0644 .env', '0644 scripts/__pycache__/a.pyc', '0644 /etc/passwd',
                         '0644 compose/mars.env', '0600 README.md', '0644 secrets/do_team_token',
                         '0644 dist/old.zip', '0644 a\\b'):
            manifest.write_text(good + bad_line + '\n')
            with self.assertRaises(mr.ReleaseError, msg=bad_line):
                self.build()
        lines = good.splitlines()
        manifest.write_text('\n'.join([lines[0], lines[2], lines[1]] + lines[3:]) + '\n')
        with self.assertRaises(mr.ReleaseError):
            self.build()
        manifest.write_text(good + '0644 docs/does-not-exist.md\n')
        with self.assertRaises(mr.ReleaseError):
            self.build()

    def test_version_commit_mode_and_output_location_rejection(self):
        lock_path = self.src / mr.LOCK
        lock = json.loads(lock_path.read_text())
        for version in ('latest', '1.88.0-mars1/../../x', '2.0.0-mars1'):
            lock_path.write_text(json.dumps({**lock, 'package_version': version}))
            with self.assertRaises(mr.ReleaseError, msg=version):
                self.build()
        lock_path.write_text(json.dumps(lock, indent=2) + '\n')
        for commit in (None, 'f0f9fc7', 'main', 'G' * 40):
            with self.assertRaises(mr.ReleaseError, msg=commit):
                self.build(commit=commit)
        with self.assertRaises(mr.ReleaseError):
            self.build(out=self.src / 'dist')
        os.chmod(self.src / 'scripts' / 'preflight.sh', 0o644)
        with self.assertRaises(mr.ReleaseError):
            self.build()

    def test_existing_different_archive_is_not_overwritten(self):
        self.out.mkdir()
        (self.out / 'openhands-enterprise-mars-1.88.0-mars1.zip').write_bytes(b'older')
        with self.assertRaises(mr.ReleaseError):
            self.build()
        self.assertEqual((self.out / 'openhands-enterprise-mars-1.88.0-mars1.zip').read_bytes(), b'older')

    def test_verify_rejects_tampering_extra_entries_and_symlinks(self):
        zip_path, _ = self.build()
        root = 'openhands-enterprise-mars-1.88.0-mars1/'
        with zipfile.ZipFile(zip_path) as zf:
            entries = [(i, zf.read(i)) for i in zf.infolist()]

        def rewrite(name, mutate):
            path = self.tmp / name
            with zipfile.ZipFile(path, 'w') as zf:
                for info, data in entries:
                    info, data = mutate(info, data)
                    zf.writestr(info, data)
                if name == 'extra.zip':
                    zf.writestr(mr.zip_entry(root + 'compose/mars.env', 0o644), b'x')
            return path

        tampered = rewrite('tampered.zip', lambda i, d: (i, d + b'#') if i.filename.endswith('README.md') else (i, d))
        extra = rewrite('extra.zip', lambda i, d: (i, d))

        def as_link(info, data):
            if info.filename.endswith('README.md'):
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            return info, data
        link = rewrite('link.zip', as_link)
        for path in (tampered, extra, link):
            with self.assertRaises(mr.ReleaseError, msg=path.name):
                mr.verify(path)

    def test_extracted_bundle_needs_no_git_and_rebuilds_identically(self):
        zip_path, digest = self.build()
        bundle = self.tmp / 'bundle'
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                target = bundle / info.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(info))
                os.chmod(target, (info.external_attr >> 16) & 0o777)
        root = bundle / 'openhands-enterprise-mars-1.88.0-mars1'
        manifest_count = len(mr.read_manifest(root))
        self.assertEqual(ms.verify_package_sums(root), manifest_count + 1)
        _, again = self.build(root=root, out=self.tmp / 'out3')
        self.assertEqual(again, digest)
        res = subprocess.run([sys.executable, str(root / 'installer' / 'mars_setup.py'), '--help'],
                             capture_output=True, text=True, env={'PATH': '/usr/bin:/bin'})
        self.assertEqual(res.returncode, 0)

    @unittest.skipUnless(HAVE_GIT, 'git not installed')
    def test_git_checkout_provenance_tracked_clean_and_modes(self):
        env = {**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't',
               'GIT_COMMITTER_EMAIL': 't@t', 'GIT_CONFIG_GLOBAL': '/dev/null'}

        def git(*args):
            return subprocess.run(['git', '-C', str(self.src), *args], check=True, capture_output=True,
                                  text=True, env=env).stdout.strip()
        git('init', '-q')
        git('add', '-A')
        git('commit', '-qm', 'snapshot')
        head = git('rev-parse', 'HEAD')
        zip_path, _ = self.build(commit=None)
        release = mr.verify(zip_path)
        self.assertEqual((release['source_commit'], release['source_commit_verified']), (head, True))
        with self.assertRaises(mr.ReleaseError):
            self.build(commit='1' * 40, out=self.tmp / 'o2')
        (self.src / 'README.md').write_text('local edit\n')
        with self.assertRaises(mr.ReleaseError):
            self.build(commit=None, out=self.tmp / 'o3')
        git('checkout', '--', 'README.md')
        git('rm', '-q', '--cached', 'docs/STATUS.md')
        with self.assertRaises(mr.ReleaseError):
            self.build(commit=None, out=self.tmp / 'o4')
        git('add', 'docs/STATUS.md')
        git('update-index', '--chmod=+x', 'README.md')
        git('commit', '-qm', 'mode')
        with self.assertRaises(mr.ReleaseError):
            self.build(commit=None, out=self.tmp / 'o5')

    def test_source_sha256sums_matches_manifest(self):
        self.assertEqual(mr.main(['sums', '--check']), 0)


if __name__ == '__main__':
    unittest.main()
