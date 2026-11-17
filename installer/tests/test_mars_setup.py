"""Stdlib tests for installer/mars_setup.py with a fake docker CLI.

  MARS_STOCK_COMPOSE=/path/to/stock-1.88.0/docker-compose.yml python3 -m unittest discover -s installer/tests -v

With MARS_STOCK_COMPOSE and a real Docker CLI, `compose config` merges run
against the real pinned stock file (no daemon needed); otherwise those tests skip.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
PKG = HERE.parents[1]
sys.path.insert(0, str(PKG / 'installer'))
import mars_setup as ms  # noqa: E402

STOCK = os.environ.get('MARS_STOCK_COMPOSE', '')
REAL_DOCKER = shutil.which('docker')
# Isolate Docker config without hiding a user-installed Compose plugin.
# Link only the trusted plugin binary; never copy registry/auth configuration.
REAL_COMPOSE_PLUGIN = shutil.which('docker-compose')
if not REAL_COMPOSE_PLUGIN:
    candidate = Path.home() / '.docker' / 'cli-plugins' / 'docker-compose'
    if candidate.is_file():
        REAL_COMPOSE_PLUGIN = str(candidate.resolve())
HAVE_STOCK = bool(STOCK and Path(STOCK).is_file())
HAVE_REAL_COMPOSE = HAVE_STOCK and bool(REAL_DOCKER)
# RFC example domains and a synthetic UUID; no account/resource identifiers.
APP = 'https://app.example.com'
RUN = 'https://runtime.example.com'
CFG = '3f2a9c1e-5b6d-4e7f-8a9b-0c1d2e3f4a5b'
PLANTED = 'planted-shell-value-should-never-be-used'
HEALTHY_PS = [
    {'Service': s, 'State': 'running', 'Health': 'healthy' if s in ms.HEALTHCHECKED else ''}
    for s in ms.BASELINE_SERVICES
]


class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status, self._body, self._headers = status, body, headers or {}

    def getheader(self, name):
        return self._headers.get(name)

    def read(self, n):
        chunk, self._body = self._body[:n], self._body[n:]
        return chunk


class FakeConnection:
    def __init__(self, response):
        self.response, self.requests, self.closed = response, [], False

    def request(self, method, path, headers):
        self.requests.append((method, path, headers))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='mars-setup-test-')).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / 'home'
        self.home.mkdir()
        if REAL_COMPOSE_PLUGIN:
            plugins = self.home / '.docker' / 'cli-plugins'
            plugins.mkdir(parents=True)
            os.symlink(REAL_COMPOSE_PLUGIN, plugins / 'docker-compose')
        bindir = self.tmp / 'bin'
        bindir.mkdir()
        os.symlink(HERE / 'fake_docker.py', bindir / 'docker')
        self.dir = self.tmp / 'install'
        self.scenario = {'projects': [], 'volumes': [], 'real_docker': REAL_DOCKER if HAVE_REAL_COMPOSE else None}
        self.save_scenario()
        patcher = mock.patch.dict(os.environ, {
            'PATH': f'{bindir}:{os.environ.get("PATH", "/usr/bin:/bin")}', 'HOME': str(self.home),
            'DB_PASS': PLANTED, 'COMPOSE_FILE': '/nonexistent.yml', 'APP_PORT': '1',
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def save_scenario(self, **updates):
        self.scenario.update(updates)
        (self.home / 'fake_docker.json').write_text(json.dumps(self.scenario))

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = ms.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def calls(self):
        path = self.home / 'docker_calls.jsonl'
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def init(self, project='marstest', expect=0, *extra):
        rc, out, err = self.cli('init', '--dir', str(self.dir), '--project', project,
                                '--app-origin', APP, '--runtime-origin', RUN, *extra)
        self.assertEqual(rc, expect, out + err)
        return out + err

    def install_stock(self):
        rc, out, err = self.cli('fetch-compose', '--dir', str(self.dir), '--from-file', STOCK)
        self.assertEqual(rc, 0, out + err)

    def secrets(self):
        inst = ms.Install(str(self.dir))
        return [v for v in inst.secret_values() if v]

    def set_site(self, key, value):
        path = self.dir / 'site.env'
        lines = [f'{key}={value}' if line.startswith(f'{key}=') else line
                 for line in path.read_text().split('\n')]
        path.write_text('\n'.join(lines))

    def prepare_mars(self):
        self.init()
        self.install_stock()
        self.set_site('DO_MARS_CONFIG_ID', CFG)
        (self.dir / 'secrets' / 'do_team_token').write_text('dop_v1_fake_token_for_tests_only')
        self.save_scenario(images=list(ms.load_lock()['local_images'].values()), ps=HEALTHY_PS, revision='213')
        rc, out, err = self.cli('build', '--dir', str(self.dir))
        self.assertEqual(rc, 0, out + err)
        for name, value in (('GATEWAY_UID', os.getuid()), ('GATEWAY_GID', os.getgid())):
            patcher = mock.patch.object(ms, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class LockAndPins(unittest.TestCase):
    def tampered(self, mutate):
        lock = json.loads(ms.LOCK_PATH.read_text())
        mutate(lock)
        path = Path(tempfile.mkdtemp()) / 'lock.json'
        self.addCleanup(shutil.rmtree, path.parent, True)
        path.write_text(json.dumps(lock))
        return path

    def test_shipped_lock_and_dockerfiles_match_immutable_pins(self):
        lock = ms.load_lock()
        self.assertEqual(lock['enterprise']['compose']['url'],
                         'https://raw.githubusercontent.com/OpenHands/enterprise/'
                         'c6191be5fb66e5ed6472062d9f6a554b58570d97/docker-compose.yml')
        ms.check_dockerfile_pins()

    def test_immutable_base_rejects_main_latest_or_other_commit(self):
        for mutate in (
            lambda lk: lk['enterprise']['compose'].update(url=ms.COMPOSE_URL.replace(ms.ENTERPRISE_COMMIT, 'main')),
            lambda lk: lk['enterprise'].update(commit='0' * 40),
            lambda lk: lk['enterprise']['compose'].update(sha256='0' * 64),
            lambda lk: lk['enterprise'].update(base_image='ghcr.io/openhands/enterprise-server:latest'),
            lambda lk: lk['mars'].update(max_sandboxes_per_identity=50),
        ):
            with self.assertRaises(ms.SetupError):
                ms.load_lock(self.tampered(mutate))

    def test_version_rejection(self):
        for version in ('latest', '1.89.0-mars1', '1.88.0', '1.88.0-mars0', '1.88.0-mars1/../x'):
            with self.assertRaises(ms.SetupError, msg=version):
                ms.load_lock(self.tampered(lambda lk, v=version: lk.update(package_version=v)))
        with self.assertRaises(ms.SetupError):
            ms.load_lock(self.tampered(lambda lk: lk['local_images'].update(app='registry.example.invalid/x:1')))

    def test_dockerfile_pin_drift_detected(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        shutil.copytree(PKG / 'docker', root / 'docker')
        path = root / 'docker' / 'enterprise.Dockerfile'
        path.write_text(path.read_text().replace('@sha256:cc67', '@sha256:dd67'))
        with self.assertRaises(ms.SetupError):
            ms.check_dockerfile_pins(root)


@unittest.skipUnless(HAVE_STOCK, 'set MARS_STOCK_COMPOSE to the pinned stock file')
class Downloader(unittest.TestCase):
    def setUp(self):
        self.good = Path(STOCK).read_bytes()
        self.dest_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dest_dir, True)
        self.dest = self.dest_dir / 'docker-compose.yml'

    def fetch(self, response, **kw):
        conn = FakeConnection(response)
        try:
            return ms.download_compose(lambda timeout: conn, **kw), conn
        finally:
            self.assertTrue(conn.closed)

    def test_good_download_requests_only_the_pinned_path_and_saves_atomically(self):
        data, conn = self.fetch(FakeResponse(200, self.good, {'Content-Length': str(len(self.good))}))
        self.assertEqual(conn.requests[0][:2], ('GET', ms.COMPOSE_PATH))
        self.assertEqual(conn.requests[0][2]['Host'], 'raw.githubusercontent.com')
        self.assertEqual(ms.install_compose(self.dest, data), 'written')
        self.assertEqual(ms.sha256_file(self.dest), ms.COMPOSE_SHA256)
        self.assertEqual(ms.install_compose(self.dest, data), 'kept')
        self.assertEqual(sorted(p.name for p in self.dest_dir.iterdir()), ['docker-compose.yml'])

    def test_hash_mismatch_writes_nothing(self):
        bad = self.good.replace(b'postgres:16', b'postgres:17')
        data, _ = self.fetch(FakeResponse(200, bad))
        with self.assertRaises(ms.SetupError):
            ms.install_compose(self.dest, data)
        self.assertEqual(list(self.dest_dir.iterdir()), [])

    def test_redirect_and_errors_refused_without_printing_body(self):
        for status in (301, 302, 307, 404, 500):
            body = b'SECRET-BODY-MARKER <a href="https://evil.example/">moved</a>'
            with self.assertRaises(ms.SetupError) as ctx:
                self.fetch(FakeResponse(status, body, {'Location': 'https://evil.example/x'}))
            self.assertIn(str(status), str(ctx.exception))
            self.assertNotIn('SECRET-BODY-MARKER', str(ctx.exception))
            self.assertNotIn('evil.example', str(ctx.exception))

    def test_size_bounds(self):
        with self.assertRaises(ms.SetupError):
            self.fetch(FakeResponse(200, b'x', {'Content-Length': str(10 ** 9)}))
        with self.assertRaises(ms.SetupError):
            self.fetch(FakeResponse(200, b'x' * (ms.MAX_COMPOSE_BYTES + 1)))

    def test_time_bound(self):
        clock = iter([0.0, 0.0, 1000.0, 1000.0])
        with mock.patch.object(ms.time, 'monotonic', lambda: next(clock)):
            with self.assertRaises(ms.SetupError):
                self.fetch(FakeResponse(200, self.good), timeout=5)

    def test_existing_different_file_is_not_overwritten(self):
        self.dest.write_bytes(b'operator copy')
        with self.assertRaises(ms.SetupError):
            ms.install_compose(self.dest, self.good)
        self.assertEqual(self.dest.read_bytes(), b'operator copy')


class EnvParsing(unittest.TestCase):
    ALLOWED = ms.SITE_REQUIRED + ms.SITE_OPTIONAL

    def test_rejects_anything_a_shell_or_compose_would_evaluate(self):
        marker = Path(tempfile.gettempdir()) / f'mars-pwn-{os.getpid()}'
        for value in (f'$(touch {marker})', f'`touch {marker}`', '${HOME}', '"quoted"', "'q'",
                      'a b', 'x;touch y', 'a#b', 'x\r'):
            with self.assertRaises(ms.SetupError, msg=value):
                ms.parse_env_text(f'APP_ORIGIN={value}\n', 'site.env', self.ALLOWED)
        self.assertFalse(marker.exists())

    def test_rejects_unknown_duplicate_and_export_lines(self):
        for text in ('PATH=/tmp\n', 'APP_PORT=1\nAPP_PORT=2\n', 'export APP_PORT=9043\n', ' APP_PORT=9043\n'):
            with self.assertRaises(ms.SetupError, msg=text):
                ms.parse_env_text(text, 'site.env', self.ALLOWED)

    def test_error_messages_never_include_values(self):
        with self.assertRaises(ms.SetupError) as ctx:
            ms.parse_env_text('DB_PASS=$super-secret-value\n', 'stack.env', ms.STACK_GENERATED)
        self.assertNotIn('super-secret-value', str(ctx.exception))

    def test_origin_validation(self):
        self.assertEqual(ms.validate_origin(APP, 'APP_ORIGIN'), 'app.example.com')
        for bad in ('http://app.example.com', 'https://app.example.invalid', 'https://localhost',
                    'https://10.0.0.1', 'https://app.example.com/', 'https://app.example.com:8443',
                    'https://APP.example.com', 'https://intranet'):
            with self.assertRaises(ms.SetupError, msg=bad):
                ms.validate_origin(bad, 'APP_ORIGIN')

    def test_redact(self):
        self.assertEqual(ms.redact('pw=abcdefgh1234 ok', ['abcdefgh1234', '']), 'pw=<redacted> ok')


class Init(Base):
    def test_init_creates_private_files_and_prints_no_secrets(self):
        out = self.init()
        self.assertEqual(oct(self.dir.stat().st_mode & 0o777), '0o700')
        for rel in ('private/stack.env', 'secrets/session_signing_key', 'secrets/init_api_key',
                    'secrets/do_team_token', 'state.json', 'site.env'):
            self.assertEqual((self.dir / rel).stat().st_mode & 0o077, 0, rel)
        self.assertEqual((self.dir / 'secrets' / 'do_team_token').read_bytes(), b'')
        stack = ms.parse_env_text((self.dir / 'private/stack.env').read_text(), 's', ms.STACK_GENERATED + ms.STACK_OPTIONAL)
        values = [stack[k] for k in ms.STACK_GENERATED]
        self.assertEqual(len(set(values)), 4)
        self.assertTrue(all(len(v) >= 32 for v in values))
        self.assertNotIn('MARS_GATEWAY_DB_USER', stack)
        for secret in self.secrets():
            self.assertNotIn(secret, out)
        state = json.loads((self.dir / 'state.json').read_text())
        self.assertEqual((state['project'], state['install_dir']), ('marstest', str(self.dir)))
        self.assertEqual(state['gateway_db_role'], 'shared-application-role')
        self.assertFalse(any('up' in c['argv'] for c in self.calls()))

    def test_retry_preserves_secrets_and_refuses_conflicting_inputs(self):
        self.init()
        before = {p: p.read_bytes() for p in self.dir.rglob('*') if p.is_file() and p.name != 'state.json'}
        out = self.init()
        self.assertIn('kept', out)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        rc, _, err = self.cli('init', '--dir', str(self.dir), '--project', 'marstest',
                              '--app-origin', 'https://other.example.com', '--runtime-origin', RUN)
        self.assertEqual(rc, 1)
        self.assertIn('APP_ORIGIN', err)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.init('otherproject', 1)

    def test_refuses_foreign_or_package_directories(self):
        self.dir.mkdir()
        (self.dir / 'notes.txt').write_text('x')
        self.assertIn('not empty', self.init(expect=1))
        for bad in (PKG / 'install', PKG.parent):
            with self.assertRaises(ms.SetupError):
                ms.Install(str(bad))
        self.init('Bad_Name', 1)

    def test_project_collision_with_existing_compose_project_or_volumes(self):
        self.save_scenario(projects=[{'Name': 'marstest', 'Status': 'running(7)',
                                      'ConfigFiles': '/srv/openhands/docker-compose.yml'}])
        self.assertIn('already exists', self.init(expect=1))
        self.assertFalse(self.dir.exists() and (self.dir / 'state.json').exists())
        self.save_scenario(projects=[], volumes=['marstest_db-data'])
        shutil.rmtree(self.dir, True)
        self.assertIn('volumes', self.init(expect=1))

    def test_copied_install_directory_is_refused(self):
        self.init()
        copy = self.tmp / 'copy'
        shutil.copytree(self.dir, copy)
        rc, _, err = self.cli('check', '--dir', str(copy), '--stage', 'baseline')
        self.assertEqual(rc, 1)


@unittest.skipUnless(HAVE_REAL_COMPOSE, 'needs MARS_STOCK_COMPOSE and a real docker CLI for compose config')
class CheckAndUp(Base):
    def compose_config(self, stage):
        inst = ms.Install(str(self.dir))
        state = inst.load_state(ms.load_lock())
        res = ms.run_docker(inst.compose_args(state, stage) + ['config', '--format', 'json'])
        self.assertEqual(res.returncode, 0)
        return json.loads(res.stdout)

    def test_baseline_check_ready_with_real_stock_compose_and_sanitized_env(self):
        self.init()
        self.install_stock()
        rc, out, err = self.cli('check', '--dir', str(self.dir), '--stage', 'baseline')
        self.assertEqual(rc, 0, out + err)
        self.assertIn('READY baseline', out)
        self.assertIn('does not prove an end-to-end install', out)
        for secret in self.secrets():
            self.assertNotIn(secret, out + err)
        for call in self.calls():
            self.assertNotIn('DB_PASS', call['env_keys'])
            self.assertNotIn('COMPOSE_FILE', call['env_keys'])
            self.assertNotIn('up', call['argv'])
        cfg = self.compose_config('baseline')
        services = cfg['services']
        self.assertEqual(services['proxy']['ports'][0]['host_ip'], '127.0.0.1')
        self.assertEqual(services['app']['environment']['OH_WEB_URL'], APP)
        self.assertNotEqual(services['app']['environment']['DB_PASS'], PLANTED)
        self.assertNotIn('mars-gateway', services)
        self.assertEqual(services['app']['image'], ms.ENTERPRISE_BASE_IMAGE)

    def test_mars_rendering_public_callback_private_api_loopback_cap(self):
        self.prepare_mars()
        rc, out, err = self.cli('check', '--dir', str(self.dir), '--stage', 'mars')
        self.assertEqual(rc, 0, out + err)
        self.assertIn('READY mars-activation', out)
        self.assertIn('shared application DB role', out)
        services = self.compose_config('mars')['services']
        auto = services['automation-server']['environment']
        self.assertEqual(auto['AUTOMATION_BASE_URL'], APP)
        self.assertEqual(auto['AUTOMATION_SANDBOX_OPENHANDS_API_URL'], APP)
        self.assertEqual(auto['AUTOMATION_OPENHANDS_API_BASE_URL'], 'http://app:3000')
        self.assertEqual(auto['AUTOMATION_SANDBOX_INTERNAL_URL'], '')
        self.assertEqual(services['app']['environment']['OH_SANDBOX_MAX_NUM_SANDBOXES'], '10')
        self.assertEqual(services['app']['image'], 'openhands-enterprise-mars:1.88.0-mars1')
        for name in ('proxy', 'mars-gateway'):
            self.assertEqual([p['host_ip'] for p in services[name]['ports']], ['127.0.0.1'])
        self.assertNotIn('example.invalid', json.dumps(services))

    def test_rendering_assertions_catch_regressions(self):
        self.prepare_mars()
        self.cli('check', '--dir', str(self.dir), '--stage', 'mars')
        inst = ms.Install(str(self.dir))
        site, stack, lock = inst.read_site(), inst.read_stack(), ms.load_lock()
        cfg = self.compose_config('mars')
        self.assertEqual(ms.verify_rendered(cfg, 'mars', site, stack, lock), [])
        for path, value, needle in (
            (('proxy', 'ports', 0, 'host_ip'), '0.0.0.0', 'non-loopback'),
            (('automation-server', 'environment', 'AUTOMATION_BASE_URL'), 'http://proxy', 'native callback'),
            (('automation-server', 'environment', 'AUTOMATION_OPENHANDS_API_BASE_URL'), APP, 'private API'),
            (('app', 'environment', 'OH_SANDBOX_MAX_NUM_SANDBOXES'), '50', 'cap'),
        ):
            broken = json.loads(json.dumps(cfg))
            node = broken['services']
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = value
            problems = ms.verify_rendered(broken, 'mars', site, stack, lock)
            self.assertTrue(any(needle in p for p in problems), (path, problems))
            for secret in self.secrets():
                self.assertNotIn(secret, ' '.join(problems))

    def test_unsupported_migration_blocks_activation(self):
        self.prepare_mars()
        for revision in ('214', 'abc123', None):
            self.save_scenario(revision=revision)
            rc, out, err = self.cli('up', '--dir', str(self.dir), '--mars', '--baseline-verified', '--backup-confirmed')
            self.assertEqual(rc, 1)
            self.assertIn('nothing was started', err)
        self.assertFalse(any('up' in c['argv'] for c in self.calls()))
        self.save_scenario(revision='214')
        self.assertIn('will not stamp, downgrade', self.cli('check', '--dir', str(self.dir), '--stage', 'mars')[1])

    def test_mars_requires_attestation_healthy_baseline_and_gateway_readable_secrets(self):
        self.prepare_mars()
        rc, _, err = self.cli('up', '--dir', str(self.dir), '--mars')
        self.assertEqual(rc, 1)
        self.assertIn('--baseline-verified', err)
        self.save_scenario(ps=[r if r['Service'] != 'app' else {**r, 'Health': 'starting'} for r in HEALTHY_PS])
        self.assertIn('baseline service app', self.cli('check', '--dir', str(self.dir), '--stage', 'mars')[1])
        self.save_scenario(ps=HEALTHY_PS)
        with mock.patch.object(ms, 'GATEWAY_UID', 42420), mock.patch.object(ms, 'GATEWAY_GID', 42420):
            out = self.cli('check', '--dir', str(self.dir), '--stage', 'mars')[1]
        self.assertIn('chown root:42420', out)
        os.chmod(self.dir / 'secrets' / 'init_api_key', 0o644)
        self.assertIn('world-accessible', self.cli('check', '--dir', str(self.dir), '--stage', 'mars')[1])
        self.assertFalse(any('up' in c['argv'] for c in self.calls()))

    def test_gateway_role_rules(self):
        self.prepare_mars()
        with open(self.dir / 'private' / 'stack.env', 'a') as fh:
            fh.write('MARS_GATEWAY_DB_USER=openhands_mars_gateway\nMARS_GATEWAY_DB_PASS=a-long-gateway-password\n')
        out = self.cli('check', '--dir', str(self.dir), '--stage', 'mars')[1]
        self.assertIn('leave it unset for the first activation', out)
        self.save_scenario(revision='do_mars_001', role_priv='110')
        self.assertIn('not least-privilege', self.cli('check', '--dir', str(self.dir), '--stage', 'mars')[1])
        self.save_scenario(role_priv='100')
        rc, out, _ = self.cli('check', '--dir', str(self.dir), '--stage', 'mars')
        self.assertEqual(rc, 0, out)
        self.assertNotIn('a-long-gateway-password', out)

    def test_up_is_explicit_isolated_and_redacted(self):
        self.init()
        self.install_stock()
        db_pass = ms.Install(str(self.dir)).read_stack()['DB_PASS']
        self.save_scenario(up_output=f'Container marstest-db-1 Started DB_PASS={db_pass}\n')
        rc, out, err = self.cli('up', '--dir', str(self.dir), '--baseline')
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn(db_pass, out + err)
        self.assertIn('<redacted>', out)
        ups = [c['argv'] for c in self.calls() if 'up' in c['argv']]
        self.assertEqual(len(ups), 1)
        argv = ups[0]
        self.assertEqual(argv[argv.index('-p') + 1], 'marstest')
        files = [argv[i + 1] for i, a in enumerate(argv) if a == '-f']
        self.assertEqual(files, [str(self.dir / 'compose' / 'docker-compose.yml'),
                                 str(self.dir / 'compose' / 'docker-compose.site.yml')])
        self.assertNotIn('-v', argv)
        self.assertNotIn('down', argv)
        state = json.loads((self.dir / 'state.json').read_text())
        self.assertIn('baseline_up_at', state)
        self.save_scenario(volumes=['marstest_db-data'], projects=[{
            'Name': 'marstest', 'Status': 'running(7)', 'ConfigFiles': ','.join(files)}])
        rc, out, _ = self.cli('check', '--dir', str(self.dir), '--stage', 'baseline')
        self.assertEqual(rc, 0, out)

    def test_edited_derived_env_and_shell_payloads_fail_closed(self):
        self.init()
        self.install_stock()
        self.assertEqual(self.cli('check', '--dir', str(self.dir), '--stage', 'baseline')[0], 0)
        with open(self.dir / 'private' / 'compose.env', 'a') as fh:
            fh.write('APP_PORT=80\n')
        self.assertIn('edited outside the installer', self.cli('check', '--dir', str(self.dir), '--stage', 'baseline')[1])
        (self.dir / 'private' / 'compose.env').unlink()
        marker = self.tmp / 'pwned'
        self.set_site('APP_PORT', f'$(touch {marker})')
        rc, out, _ = self.cli('check', '--dir', str(self.dir), '--stage', 'baseline')
        self.assertEqual(rc, 1)
        self.assertFalse(marker.exists())
        self.assertNotIn('touch', out)

    def test_render_env_spec_is_private_and_not_printed(self):
        self.init()
        rc, out, err = self.cli('render-env-spec', '--dir', str(self.dir))
        self.assertEqual(rc, 0, out + err)
        spec = self.dir / 'private' / 'environment.yaml'
        self.assertEqual(spec.stat().st_mode & 0o077, 0)
        text = spec.read_text()
        init_key = (self.dir / 'secrets' / 'init_api_key').read_text()
        self.assertIn(init_key, text)
        self.assertIn('  - app.example.com', text)
        self.assertNotIn('example.invalid', text)
        self.assertNotIn(init_key, out + err)
        self.assertEqual(self.cli('render-env-spec', '--dir', str(self.dir))[0], 0)

    def test_partial_first_up_after_volume_creation_can_be_retried_safely(self):
        self.init()
        self.install_stock()
        self.save_scenario(up_rc=1)
        rc, _, err = self.cli('up', '--dir', str(self.dir), '--baseline')
        self.assertEqual(rc, 1)
        self.assertIn('nothing was removed or reset', err)
        state = json.loads((self.dir / 'state.json').read_text())
        self.assertNotIn('baseline_up_at', state)
        self.assertEqual(state['compose_owned_intent']['project'], 'marstest')
        owned = [str(f) for f in ms.Install(str(self.dir)).config_files('baseline')]
        # Compose created the volumes and some containers before failing.
        self.save_scenario(up_rc=0, volumes=['marstest_db-data'],
                           projects=[{'Name': 'marstest', 'Status': 'exited(2)', 'ConfigFiles': ','.join(owned)}])
        rc, out, err = self.cli('up', '--dir', str(self.dir), '--baseline')
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(len([c for c in self.calls() if 'up' in c['argv']]), 2)
        # The intent never covers another deployment's Compose files.
        self.save_scenario(projects=[{'Name': 'marstest', 'Status': 'running(7)',
                                      'ConfigFiles': '/srv/other/docker-compose.yml'}])
        self.assertIn('other Compose files', self.cli('check', '--dir', str(self.dir), '--stage', 'baseline')[1])

    def test_foreign_volumes_still_refused_without_intent(self):
        self.init()
        self.install_stock()
        self.save_scenario(volumes=['marstest_db-data'])
        rc, out, err = self.cli('up', '--dir', str(self.dir), '--baseline')
        self.assertEqual(rc, 1)
        self.assertIn('were not created by this install', out)
        self.assertFalse(any('up' in c['argv'] for c in self.calls()))

    def test_baseline_up_refused_after_mars_activation_or_non_213_db(self):
        self.prepare_mars()
        rc, out, err = self.cli('up', '--dir', str(self.dir), '--mars', '--baseline-verified', '--backup-confirmed')
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(len([c for c in self.calls() if 'up' in c['argv']]), 1)
        self.save_scenario(revision='do_mars_001')
        rc, _, err = self.cli('up', '--dir', str(self.dir), '--baseline')
        self.assertEqual(rc, 1)
        self.assertIn('ROLLBACK.md', err)
        self.assertEqual(len([c for c in self.calls() if 'up' in c['argv']]), 1)
        # Intent alone (activation attempted, Compose failed) also blocks stock images.
        state = json.loads((self.dir / 'state.json').read_text())
        state.pop('mars_up_at')
        ms.Install(str(self.dir)).save_state(state)
        self.assertEqual(self.cli('up', '--dir', str(self.dir), '--baseline')[0], 1)
        self.assertEqual(len([c for c in self.calls() if 'up' in c['argv']]), 1)

    def test_baseline_restart_refused_when_db_not_at_213(self):
        self.init()
        self.install_stock()
        self.assertEqual(self.cli('up', '--dir', str(self.dir), '--baseline')[0], 0)
        for revision in ('do_mars_001', '214', None):
            self.save_scenario(revision=revision)
            rc, _, err = self.cli('up', '--dir', str(self.dir), '--baseline')
            self.assertEqual(rc, 1, revision)
            self.assertIn('refusing to (re)start stock images', err)
        self.assertEqual(len([c for c in self.calls() if 'up' in c['argv']]), 1)
        self.save_scenario(revision='213')
        self.assertEqual(self.cli('up', '--dir', str(self.dir), '--baseline')[0], 0)

    def test_mars_check_requires_the_image_ids_this_install_built(self):
        self.prepare_mars()
        self.assertEqual(self.cli('check', '--dir', str(self.dir), '--stage', 'mars')[0], 0)
        tag = ms.load_lock()['local_images']['app']
        self.save_scenario(image_ids={tag: 'sha256:' + 'cd' * 32})
        out = self.cli('check', '--dir', str(self.dir), '--stage', 'mars')[1]
        self.assertIn('points to a different image', out)
        rc, _, err = self.cli('up', '--dir', str(self.dir), '--mars', '--baseline-verified', '--backup-confirmed')
        self.assertEqual(rc, 1)
        self.assertFalse(any('up' in c['argv'] for c in self.calls()))
        self.save_scenario(image_ids={tag: 'not-an-id'})
        rc, _, err = self.cli('build', '--dir', str(self.dir))
        self.assertEqual(rc, 1)
        self.assertIn('could not record the image ID', err)
        self.assertNotIn('built_images', json.loads((self.dir / 'state.json').read_text()))
        self.assertIn('no build of', self.cli('check', '--dir', str(self.dir), '--stage', 'mars')[1])

    def test_intended_permission_shape_is_ready_and_root_owned_dir_fails_cleanly(self):
        """Group path: dir owned by the installer user, group = gateway gid, 0750; token/signing 0640.

        Non-root tests cannot chown to root:42420, so a group the test user belongs to stands in for
        42420 and the gateway uid is someone else. Owner of the two files stays the test user here;
        on a real host they are root-owned and only group-readable by 42420.
        """
        self.prepare_mars()
        groups = [g for g in os.getgroups() if g != os.getgid()] or [os.getgid()]
        gid = groups[0]
        secrets_dir = self.dir / 'secrets'
        with mock.patch.object(ms, 'GATEWAY_UID', 42420), mock.patch.object(ms, 'GATEWAY_GID', gid):
            for name in ('do_team_token', 'session_signing_key'):
                os.chown(secrets_dir / name, -1, gid)
                os.chmod(secrets_dir / name, 0o640)
            os.chmod(secrets_dir / 'init_api_key', 0o600)
            os.chown(secrets_dir, -1, gid)
            os.chmod(secrets_dir, 0o750)
            rc, out, _ = self.cli('check', '--dir', str(self.dir), '--stage', 'mars')
            self.assertEqual(rc, 0, out)
            os.chmod(secrets_dir, 0o700)
            self.assertIn('not traversable by gid', self.cli('check', '--dir', str(self.dir), '--stage', 'mars')[1])
            os.chmod(secrets_dir, 0o750)
            os.chmod(secrets_dir / 'session_signing_key', 0o600)
            self.assertIn('chmod 0640 on this file only', self.cli('check', '--dir', str(self.dir), '--stage', 'mars')[1])
            os.chmod(secrets_dir / 'session_signing_key', 0o640)
            # Stand-in for a root-owned 0750 dir the installer user cannot search: no crash, a FAIL line.
            os.chmod(secrets_dir, 0o000)
            self.addCleanup(os.chmod, secrets_dir, 0o700)
            rc, out, err = self.cli('check', '--dir', str(self.dir), '--stage', 'mars')
            os.chmod(secrets_dir, 0o750)
            self.assertEqual(rc, 1)
            self.assertIn('cannot stat the secret files', out)
            self.assertNotIn('Traceback', out + err)


if __name__ == '__main__':
    unittest.main()
