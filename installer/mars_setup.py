#!/usr/bin/env python3
"""Staged, stdlib-only setup for the MARS evaluation package on Linux.

  python3 installer/mars_setup.py init --dir DIR --project NAME --app-origin URL --runtime-origin URL
  python3 installer/mars_setup.py fetch-compose --dir DIR [--from-file PATH]
  python3 installer/mars_setup.py check --dir DIR --stage baseline|mars
  python3 installer/mars_setup.py up --dir DIR --baseline
  python3 installer/mars_setup.py build --dir DIR
  python3 installer/mars_setup.py render-env-spec --dir DIR
  python3 installer/mars_setup.py up --dir DIR --mars --baseline-verified --backup-confirmed
  python3 installer/mars_setup.py status --dir DIR
  python3 installer/mars_setup.py compose-command --dir DIR --stage baseline|mars

See docs/INSTALLER_CONTRACT.md. Env files are parsed as plain KEY=value data;
nothing is ever sourced, evaluated or passed to a shell. Secret values are
never accepted as flags and never printed.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import http.client
import json
import os
import re
import secrets
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = PACKAGE_ROOT / 'installer' / 'release.lock.json'
SITE_OVERLAY_SRC = PACKAGE_ROOT / 'installer' / 'compose' / 'docker-compose.site.yml'
MARS_OVERLAY_SRC = PACKAGE_ROOT / 'compose' / 'docker-compose.mars.yml'
CANVAS_OVERLAY_SRC = PACKAGE_ROOT / 'compose' / 'docker-compose.canvas-pilot.yml'
ENV_SPEC_TEMPLATE = PACKAGE_ROOT / 'templates' / 'environment.example.yaml'

# Immutable pins. release.lock.json must agree with every one of them.
ENTERPRISE_VERSION = '1.88.0'
ENTERPRISE_COMMIT = 'c6191be5fb66e5ed6472062d9f6a554b58570d97'
COMPOSE_HOST = 'raw.githubusercontent.com'
COMPOSE_PATH = f'/OpenHands/enterprise/{ENTERPRISE_COMMIT}/docker-compose.yml'
COMPOSE_URL = f'https://{COMPOSE_HOST}{COMPOSE_PATH}'
COMPOSE_SHA256 = 'd17ec0f72978b50f0d1df666e9ad8c2e7cdd9b2938601b52b3a4dcfb97d7ac55'
COMPOSE_SIZE = 9539
ENTERPRISE_BASE_IMAGE = (
    'ghcr.io/openhands/enterprise-server:1.88.0'
    '@sha256:cc67d10163e9e466b65d7a6e765a020fcc23ca036e78c6a6d846c458d1e2d9eb'
)
AUTOMATION_BASE_IMAGE = (
    'ghcr.io/openhands/automation:1.30.0'
    '@sha256:fb722b6fcde1a7fa67a15c40eb83af0ca23f1c111d0bc745f3b77a8b1bd33afa'
)
MIGRATION_BASE = '213'
# The prior package's terminal revision: an existing install upgrading from
# before the warm-pool migration lands here until the MARS app's next start
# runs do_mars_002 automatically (same transient-state handling already used
# for MIGRATION_BASE itself, below).
MIGRATION_PACKAGE_PREV = 'do_mars_001'
MIGRATION_PACKAGE = 'do_mars_002'
MAX_SANDBOXES = 10
GATEWAY_UID = 42420
GATEWAY_GID = 42420
MIN_PYTHON = (3, 11)
MIN_COMPOSE = (2, 24, 4)
MAX_COMPOSE_BYTES = 64 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 30
STATE_SCHEMA = 1

PACKAGE_VERSION_RE = re.compile(r'^1\.88\.0-mars[1-9][0-9]{0,3}$')
LOCAL_TAG_RE = re.compile(r'^[a-z0-9][a-z0-9._-]{0,127}:[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
PROJECT_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,47}$')
HOST_RE = re.compile(r'^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$')
UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
ROLE_RE = re.compile(r'^[a-z_][a-z0-9_]{0,62}$')
REVISION_RE = re.compile(r'^[A-Za-z0-9_]{1,64}$')
DIR_RE = re.compile(r'^/[A-Za-z0-9._/-]+$')
ENV_LINE_RE = re.compile(r'^([A-Z][A-Z0-9_]*)=([A-Za-z0-9._:/@+,=-]*)$')
RESERVED_HOST_SUFFIXES = ('.invalid', '.example', '.test', '.localhost', '.local', '.internal')

SITE_REQUIRED = ('APP_ORIGIN', 'RUNTIME_ORIGIN', 'DO_MARS_CONFIG_ID', 'APP_PORT', 'MARS_GATEWAY_HOST_PORT')
SITE_OPTIONAL = (
    'ENABLE_POSTHOG_ANALYTICS',
    'POSTHOG_CLIENT_KEY',
    'DO_MARS_SESSION_TTL_SECONDS',
    'DO_MARS_ENABLE_BROWSER_PREVIEW',
    'AUTOMATION_RUN_KEY_TTL_SECONDS',
    'OH_SANDBOX_LIFECYCLE_IDLE_SECONDS',
    # Opt-in warm pool (do_mars_pool_config.py) and Canvas pilot bundle
    # (docs/CANVAS.md). Both are inert unless set; see validate_site and
    # Install.config_files/compose_args for how each is wired into check/up.
    'DO_MARS_POOL_ENABLED',
    'DO_MARS_POOL_SANDBOX_SPEC_ID',
    'DO_MARS_POOL_MIN_HOT',
    'DO_MARS_POOL_MIN_PAUSED_RESERVE',
    'DO_MARS_POOL_MAX_TOTAL',
    'DO_MARS_POOL_RECONCILE_INTERVAL_SECONDS',
    'CANVAS_PILOT_BUILD',
)
# Read only by render-env-spec; never forwarded into the Compose environment.
SITE_MANIFEST_ONLY = ('MARS_LLM_EGRESS_HOSTS',)
SITE_KEYS = SITE_REQUIRED + SITE_OPTIONAL + SITE_MANIFEST_ONLY
MAX_LLM_EGRESS_HOSTS = 8
STACK_GENERATED = ('DB_PASS', 'AUTOMATION_SERVICE_KEY', 'AUTOMATION_WEBHOOK_SECRET', 'AUTOMATION_KV_SECRET')
STACK_OPTIONAL = ('OPENHANDS_LICENSE_KEY', 'LICENSE_SERVER_URL', 'MARS_GATEWAY_DB_USER', 'MARS_GATEWAY_DB_PASS')
STACK_SECRET_KEYS = STACK_GENERATED + ('OPENHANDS_LICENSE_KEY', 'MARS_GATEWAY_DB_PASS')
STOCK_DEFAULT_SECRETS = {
    'openhands',
    'local-compose-service-key',
    'local-compose-webhook-secret',
    'local-compose-kv-secret',
}
SECRET_FILES = {
    # name: (minimum bytes, gateway must read)
    'do_team_token': (1, True),
    'session_signing_key': (32, True),
    'init_api_key': (32, False),
}
BASELINE_SERVICES = ('db', 'redis', 'app', 'worker', 'automation-server', 'canvas-server', 'proxy')
HEALTHCHECKED = ('db', 'redis', 'app', 'automation-server', 'canvas-server')
DOCKER_ENV_KEYS = (
    'PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR', 'XDG_RUNTIME_DIR', 'DOCKER_HOST', 'DOCKER_CONTEXT',
    'DOCKER_CONFIG', 'DOCKER_CERT_PATH', 'DOCKER_TLS_VERIFY', 'BUILDX_CONFIG',
)


class SetupError(Exception):
    """A refusal. Messages name files, keys and checks, never secret values."""


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def version_tuple(text: str) -> tuple[int, ...]:
    m = re.match(r'^v?(\d+)\.(\d+)\.(\d+)', text.strip())
    if not m:
        raise SetupError('could not parse a version number')
    return tuple(int(x) for x in m.groups())


# ---------------------------------------------------------------- lock file

def load_lock(path: Path = LOCK_PATH) -> dict:
    try:
        lock = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise SetupError(f'cannot read release lock {path.name}: {type(exc).__name__}') from None
    expected = {
        ('schema',): 1,
        ('enterprise', 'version'): ENTERPRISE_VERSION,
        ('enterprise', 'commit'): ENTERPRISE_COMMIT,
        ('enterprise', 'compose', 'url'): COMPOSE_URL,
        ('enterprise', 'compose', 'sha256'): COMPOSE_SHA256,
        ('enterprise', 'compose', 'size'): COMPOSE_SIZE,
        ('enterprise', 'base_image'): ENTERPRISE_BASE_IMAGE,
        ('enterprise', 'migration_base'): MIGRATION_BASE,
        ('enterprise', 'migration_package'): MIGRATION_PACKAGE,
        ('automation', 'base_image'): AUTOMATION_BASE_IMAGE,
        ('requirements', 'docker_compose'): '.'.join(map(str, MIN_COMPOSE)),
        ('requirements', 'python'): '.'.join(map(str, MIN_PYTHON)),
        ('mars', 'max_sandboxes_per_identity'): MAX_SANDBOXES,
        ('mars', 'gateway_uid'): 42420,
        ('mars', 'gateway_gid'): 42420,
    }
    for keys, want in expected.items():
        node = lock
        for k in keys:
            node = node.get(k) if isinstance(node, dict) else None
        if node != want:
            raise SetupError(f'release lock field {".".join(keys)} differs from the immutable pin')
    if not PACKAGE_VERSION_RE.match(str(lock.get('package_version', ''))):
        raise SetupError('release lock package_version is not a supported 1.88.0-marsN version')
    images = lock.get('local_images', {})
    for name in ('app', 'automation'):
        tag = images.get(name, '')
        if not isinstance(tag, str) or not LOCAL_TAG_RE.match(tag):
            raise SetupError(f'release lock local_images.{name} must be a registry-less local tag')
    return lock


def check_dockerfile_pins(root: Path = PACKAGE_ROOT) -> None:
    for name, arg, want in (
        ('enterprise.Dockerfile', 'ENTERPRISE_BASE', ENTERPRISE_BASE_IMAGE),
        ('automation.Dockerfile', 'AUTOMATION_BASE', AUTOMATION_BASE_IMAGE),
    ):
        text = (root / 'docker' / name).read_text(encoding='utf-8')
        found = re.findall(rf'^ARG {arg}=(\S+)$', text, re.M)
        if found != [want]:
            raise SetupError(f'docker/{name} base image pin differs from the release lock')


def verify_package_sums(root: Path = PACKAGE_ROOT) -> int:
    """Check SHA256SUMS against the package; returns the file count."""
    sums = root / 'SHA256SUMS'
    if not sums.is_file():
        raise SetupError('package SHA256SUMS is missing')
    listed: set[str] = set()
    for n, line in enumerate(sums.read_text(encoding='utf-8').splitlines(), 1):
        if not line.strip():
            continue
        m = re.match(r'^([0-9a-f]{64})  (\S+)$', line)
        if not m or '..' in m.group(2).split('/') or m.group(2).startswith('/'):
            raise SetupError(f'SHA256SUMS line {n} is malformed')
        path = root / m.group(2)
        if path.is_symlink() or not path.is_file() or sha256_file(path) != m.group(1):
            raise SetupError(f'package file {m.group(2)} does not match SHA256SUMS')
        listed.add(m.group(2))
    if not listed:
        raise SetupError('package SHA256SUMS lists no files')
    manifest = root / 'installer' / 'release-files.txt'
    for line in manifest.read_text(encoding='utf-8').splitlines() if manifest.is_file() else ['missing']:
        if line and not line.startswith('#') and line.split(' ', 1)[-1] not in listed:
            raise SetupError('SHA256SUMS does not cover every file in installer/release-files.txt')
    return len(listed)


# ------------------------------------------------------------ env files

def parse_env_text(text: str, name: str, allowed: tuple[str, ...]) -> dict[str, str]:
    """Plain KEY=value parser. Rejects anything Compose could interpolate."""
    values: dict[str, str] = {}
    for n, line in enumerate(text.split('\n'), 1):
        if line == '' or line.startswith('#'):
            continue
        m = ENV_LINE_RE.match(line)
        if not m:
            raise SetupError(
                f'{name} line {n}: expected KEY=value with a plain value '
                '(no quotes, spaces, $, backticks, # or CR)'
            )
        key, value = m.groups()
        if key not in allowed:
            raise SetupError(f'{name} line {n}: key {key} is not supported here')
        if key in values:
            raise SetupError(f'{name} line {n}: duplicate key {key}')
        values[key] = value
    return values


def read_env_file(path: Path, allowed: tuple[str, ...], private: bool) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise SetupError(f'{path.name} is missing or not a regular file')
    st = path.stat()
    if private and (st.st_mode & 0o077 or st.st_uid != os.geteuid()):
        raise SetupError(f'{path.name} must be owned by you with mode 0600 (it holds secrets)')
    try:
        text = path.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        raise SetupError(f'{path.name} is not UTF-8') from None
    return parse_env_text(text, path.name, allowed)


def validate_origin(value: str, key: str) -> str:
    m = re.match(r'^https://([^/:?#]+)$', value or '')
    host = m.group(1) if m else ''
    if not m or not HOST_RE.match(host):
        raise SetupError(f'{key} must be a public https://hostname origin (lowercase, no port, path or slash)')
    if host.endswith(RESERVED_HOST_SUFFIXES) or re.match(r'^[0-9.]+$', host):
        raise SetupError(f'{key} uses a reserved, local or placeholder hostname')
    return host


def validate_llm_egress_hosts(value: str, already_allowed: tuple[str, ...]) -> list[str]:
    """Exact extra guest egress hostnames, in order, minus ones already allowed.

    Operators list the hostname(s) their resolved LLM gateway/provider actually
    uses; nothing is inferred from a model name. Empty is valid.
    """
    key = 'MARS_LLM_EGRESS_HOSTS'
    if value == '':
        return []
    items = value.split(',')
    if len(items) > MAX_LLM_EGRESS_HOSTS:
        raise SetupError(f'{key} lists more than {MAX_LLM_EGRESS_HOSTS} hostnames')
    hosts: list[str] = []
    for n, host in enumerate(items, 1):
        if not host:
            raise SetupError(f'{key} item {n} is empty (use plain comma-separated hostnames)')
        if not HOST_RE.match(host):
            raise SetupError(
                f'{key} item {n} must be an exact lowercase public DNS hostname '
                '(no scheme, path, port, credentials, wildcard or IP address)'
            )
        if host.endswith(RESERVED_HOST_SUFFIXES):
            raise SetupError(f'{key} item {n} uses a reserved, local or placeholder hostname')
        if host not in hosts and host not in already_allowed:
            hosts.append(host)
    return hosts


def validate_port(value: str, key: str) -> int:
    if not re.match(r'^[0-9]{4,5}$', value or '') or not 1024 <= int(value) <= 65535:
        raise SetupError(f'{key} must be a port between 1024 and 65535')
    return int(value)


def validate_site(site: dict[str, str], stage: str) -> dict[str, str]:
    for key in SITE_REQUIRED:
        if key not in site:
            raise SetupError(f'site.env is missing {key}')
    app_host = validate_origin(site['APP_ORIGIN'], 'APP_ORIGIN')
    runtime_host = validate_origin(site['RUNTIME_ORIGIN'], 'RUNTIME_ORIGIN')
    if app_host == runtime_host:
        raise SetupError('RUNTIME_ORIGIN must use a hostname different from APP_ORIGIN')
    if validate_port(site['APP_PORT'], 'APP_PORT') == validate_port(
        site['MARS_GATEWAY_HOST_PORT'], 'MARS_GATEWAY_HOST_PORT'
    ):
        raise SetupError('APP_PORT and MARS_GATEWAY_HOST_PORT must differ')
    config_id = site['DO_MARS_CONFIG_ID']
    if config_id and (not UUID_RE.match(config_id) or set(config_id) <= {'0', '-'}):
        raise SetupError('DO_MARS_CONFIG_ID must be the lowercase UUID of your MARS environment config')
    if stage == 'mars' and not config_id:
        raise SetupError('DO_MARS_CONFIG_ID is empty; set it to your operator-created MARS config')
    for key, low, high in (
        ('DO_MARS_SESSION_TTL_SECONDS', 300, 86400),
        ('AUTOMATION_RUN_KEY_TTL_SECONDS', 0, 86400),
        ('OH_SANDBOX_LIFECYCLE_IDLE_SECONDS', 60, 604800),
        # Conservative, matching compose/mars.env.example's own ${VAR:-0}/
        # ${VAR:-30} defaults: unset means the pool stays fully inert.
        ('DO_MARS_POOL_MIN_HOT', 0, 100000),
        ('DO_MARS_POOL_MIN_PAUSED_RESERVE', 0, 100000),
        ('DO_MARS_POOL_MAX_TOTAL', 0, 100000),
        ('DO_MARS_POOL_RECONCILE_INTERVAL_SECONDS', 1, 86400),
    ):
        if key in site and (not site[key].isdigit() or not low <= int(site[key]) <= high):
            raise SetupError(f'{key} must be an integer in {low}..{high}')
    if site.get('ENABLE_POSTHOG_ANALYTICS', 'true') not in ('true', 'false'):
        raise SetupError('ENABLE_POSTHOG_ANALYTICS must be true or false')
    if site.get('DO_MARS_ENABLE_BROWSER_PREVIEW', 'false') not in ('true', 'false'):
        raise SetupError('DO_MARS_ENABLE_BROWSER_PREVIEW must be true or false')
    if site.get('DO_MARS_POOL_ENABLED', 'false') not in ('true', 'false'):
        raise SetupError('DO_MARS_POOL_ENABLED must be true or false')
    pool_enabled = site.get('DO_MARS_POOL_ENABLED', 'false') == 'true'
    if pool_enabled and not site.get('DO_MARS_POOL_SANDBOX_SPEC_ID'):
        raise SetupError('DO_MARS_POOL_SANDBOX_SPEC_ID is required when DO_MARS_POOL_ENABLED is true')
    min_hot = int(site.get('DO_MARS_POOL_MIN_HOT', '0'))
    min_paused = int(site.get('DO_MARS_POOL_MIN_PAUSED_RESERVE', '0'))
    max_total = int(site.get('DO_MARS_POOL_MAX_TOTAL', '0'))
    if pool_enabled and max_total < min_hot + min_paused:
        raise SetupError(
            'DO_MARS_POOL_MAX_TOTAL must be >= DO_MARS_POOL_MIN_HOT + DO_MARS_POOL_MIN_PAUSED_RESERVE '
            f'({min_hot + min_paused}), got {max_total}'
        )
    canvas_dir = site.get('CANVAS_PILOT_BUILD', '')
    if canvas_dir:
        if not DIR_RE.match(canvas_dir):
            raise SetupError('CANVAS_PILOT_BUILD must be an absolute path (letters, digits, . _ - and /)')
        build_dir = Path(canvas_dir)
        if not build_dir.is_dir():
            raise SetupError(f'CANVAS_PILOT_BUILD {canvas_dir} is not a directory')
        index = build_dir / 'index.html'
        try:
            readable = index.is_file() and os.access(index, os.R_OK)
        except OSError:
            readable = False
        if not readable:
            raise SetupError(
                f'CANVAS_PILOT_BUILD {canvas_dir} has no readable index.html '
                '(not a `npm run build` output? see docs/CANVAS.md)'
            )
    llm_hosts = validate_llm_egress_hosts(
        site.get('MARS_LLM_EGRESS_HOSTS', ''), (app_host, runtime_host)
    )
    return {'app_host': app_host, 'runtime_host': runtime_host, 'llm_egress_hosts': llm_hosts}


def validate_stack(stack: dict[str, str]) -> None:
    for key in STACK_GENERATED:
        if len(stack.get(key, '')) < 32 or stack[key] in STOCK_DEFAULT_SECRETS:
            raise SetupError(f'stack.env {key} must be a generated value of at least 32 characters')
    role = stack.get('MARS_GATEWAY_DB_USER', '')
    if role:
        if not ROLE_RE.match(role) or role == 'openhands':
            raise SetupError('MARS_GATEWAY_DB_USER must be a dedicated lowercase role name')
        if len(stack.get('MARS_GATEWAY_DB_PASS', '')) < 16:
            raise SetupError('MARS_GATEWAY_DB_PASS must be set (16+ characters) when MARS_GATEWAY_DB_USER is set')
    elif stack.get('MARS_GATEWAY_DB_PASS'):
        raise SetupError('MARS_GATEWAY_DB_PASS is set without MARS_GATEWAY_DB_USER')


def redact(text: str, values) -> str:
    for value in sorted({v for v in values if v and len(v) >= 8}, key=len, reverse=True):
        text = text.replace(value, '<redacted>')
    return text


# ------------------------------------------------------------ file writes

def atomic_write(path: Path, data: bytes, mode: int) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def create_exclusive(path: Path, data: bytes, mode: int) -> None:
    """Create a new file; never replaces an existing one."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.fchmod(fd, mode)
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def write_if_absent_or_same(path: Path, data: bytes, mode: int) -> str:
    if path.is_symlink():
        raise SetupError(f'{path.name} is a symlink; refusing')
    if path.exists():
        if path.read_bytes() != data:
            raise SetupError(f'{path} exists and differs; refusing to overwrite it')
        return 'kept'
    create_exclusive(path, data, mode)
    return 'created'


# ------------------------------------------------------------ downloader

def https_connection(timeout: float) -> http.client.HTTPSConnection:
    context = ssl.create_default_context()
    return http.client.HTTPSConnection(COMPOSE_HOST, 443, timeout=timeout, context=context)


def download_compose(connect=https_connection, timeout: float = DOWNLOAD_TIMEOUT_SECONDS,
                     max_bytes: int = MAX_COMPOSE_BYTES) -> bytes:
    """GET the one pinned file. No redirects, bounded size and time; body never printed."""
    deadline = time.monotonic() + timeout
    conn = connect(timeout)
    try:
        conn.request('GET', COMPOSE_PATH, headers={
            'Host': COMPOSE_HOST, 'User-Agent': 'openhands-mars-setup/1', 'Accept-Encoding': 'identity',
        })
        resp = conn.getresponse()
        if resp.status != 200:
            raise SetupError(f'download refused: HTTP {resp.status} (redirects and errors are not followed)')
        length = resp.getheader('Content-Length')
        if length is not None and (not length.isdigit() or int(length) > max_bytes):
            raise SetupError('download refused: Content-Length missing bound or too large')
        chunks, total = [], 0
        while True:
            if time.monotonic() > deadline:
                raise SetupError('download refused: time limit exceeded')
            chunk = resp.read(8192)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise SetupError('download refused: body exceeds the size limit')
            chunks.append(chunk)
        return b''.join(chunks)
    except (OSError, http.client.HTTPException) as exc:
        raise SetupError(f'download failed: {type(exc).__name__}') from None
    finally:
        conn.close()


def verify_compose_bytes(data: bytes) -> None:
    digest = sha256_bytes(data)
    if len(data) != COMPOSE_SIZE or digest != COMPOSE_SHA256:
        raise SetupError(
            f'stock Compose hash mismatch: expected {COMPOSE_SHA256} ({COMPOSE_SIZE} bytes), '
            f'got {digest} ({len(data)} bytes); nothing was written'
        )


def install_compose(dest: Path, data: bytes) -> str:
    verify_compose_bytes(data)
    if dest.is_symlink():
        raise SetupError(f'{dest.name} is a symlink; refusing')
    if dest.exists():
        if sha256_file(dest) == COMPOSE_SHA256:
            return 'kept'
        raise SetupError(f'{dest} exists with a different hash; refusing to overwrite it')
    atomic_write(dest, data, 0o644)
    return 'written'


# ------------------------------------------------------------ docker

def docker_env() -> dict[str, str]:
    return {k: os.environ[k] for k in DOCKER_ENV_KEYS if k in os.environ}


def run_docker(args: list[str], *, capture: bool = True, timeout: float = 120) -> subprocess.CompletedProcess:
    exe = shutil.which('docker')
    if not exe:
        raise SetupError('docker CLI not found on PATH; install Docker Engine and the Compose plugin yourself')
    try:
        return subprocess.run(
            [exe, *args], env=docker_env(), stdin=subprocess.DEVNULL, capture_output=capture,
            text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        raise SetupError(f'docker {args[0]} timed out') from None


def compose_version() -> tuple[int, ...]:
    res = run_docker(['compose', 'version', '--short'])
    if res.returncode != 0:
        raise SetupError('docker compose plugin not available')
    return version_tuple(res.stdout)


def parse_json_lines(text: str) -> list[dict]:
    text = text.strip()
    if not text:
        return []
    if text.startswith('['):
        data = json.loads(text)
        return data if isinstance(data, list) else []
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ------------------------------------------------------------ install dir

class Install:
    def __init__(self, directory: str):
        path = Path(os.path.abspath(directory)).resolve()
        if not DIR_RE.match(str(path)) or path == Path('/'):
            raise SetupError('install directory must be an absolute path of letters, digits, . _ / -')
        pkg = PACKAGE_ROOT.resolve()
        if path == pkg or pkg in path.parents or path in pkg.parents:
            raise SetupError('install directory must be outside the package checkout (and not contain it)')
        self.dir = path
        self.state_path = path / 'state.json'
        self.site_env = path / 'site.env'
        self.private = path / 'private'
        self.stack_env = self.private / 'stack.env'
        self.compose_env = self.private / 'compose.env'
        self.env_spec = self.private / 'environment.yaml'
        self.compose_dir = path / 'compose'
        self.stock = self.compose_dir / 'docker-compose.yml'
        self.site_overlay = self.compose_dir / 'docker-compose.site.yml'
        self.mars_overlay = self.compose_dir / 'docker-compose.mars.yml'
        self.canvas_overlay = self.compose_dir / 'docker-compose.canvas-pilot.yml'
        self.secrets_dir = path / 'secrets'

    # state ------------------------------------------------------------
    def load_state(self, lock: dict) -> dict:
        if self.state_path.is_symlink() or not self.state_path.is_file():
            raise SetupError(f'{self.dir} has no installer state; run init first')
        try:
            state = json.loads(self.state_path.read_text(encoding='utf-8'))
        except ValueError:
            raise SetupError('state.json is not valid JSON') from None
        if state.get('schema') != STATE_SCHEMA:
            raise SetupError('state.json schema is not supported')
        if state.get('install_dir') != str(self.dir):
            raise SetupError('state.json was written for a different directory; do not copy install directories')
        if not PROJECT_RE.match(str(state.get('project', ''))):
            raise SetupError('state.json project name is invalid')
        if state.get('package_version') != lock['package_version']:
            raise SetupError(
                f'install was created by package {state.get("package_version")}, '
                f'this package is {lock["package_version"]}; upgrades are not supported by this installer'
            )
        return state

    def save_state(self, state: dict) -> None:
        state['updated_at'] = utc_now()
        atomic_write(self.state_path, (json.dumps(state, indent=2, sort_keys=True) + '\n').encode(), 0o600)

    # env ------------------------------------------------------------------
    def read_site(self) -> dict[str, str]:
        return read_env_file(self.site_env, SITE_KEYS, private=False)

    def read_stack(self) -> dict[str, str]:
        stack = read_env_file(self.stack_env, STACK_GENERATED + STACK_OPTIONAL, private=True)
        validate_stack(stack)
        return stack

    def secret_values(self) -> list[str]:
        values = []
        try:
            stack = parse_env_text(self.stack_env.read_text(encoding='utf-8'), 'stack.env',
                                   STACK_GENERATED + STACK_OPTIONAL)
            values += [stack[k] for k in STACK_SECRET_KEYS if k in stack]
        except (OSError, SetupError, UnicodeDecodeError):
            pass
        for name in SECRET_FILES:
            try:
                values.append((self.secrets_dir / name).read_text(encoding='utf-8').strip())
            except (OSError, UnicodeDecodeError):
                pass
        return values

    def render_derived(self, lock: dict, state: dict, site: dict[str, str]) -> str:
        origin = site['APP_ORIGIN']
        lines = [
            '# Derived by mars_setup.py from site.env and release.lock.json. Do not edit;',
            '# edit site.env and re-run check. Contains no secrets.',
            f'COMPOSE_PROJECT_NAME={state["project"]}',
            f'APP_PORT={site["APP_PORT"]}',
            f'WEB_HOST={validate_origin(origin, "APP_ORIGIN")}',
            f'WEB_URL={origin}',
            f'AGENT_CANVAS_LOCK_TO_CLOUD={origin}',
            f'APP_IMAGE={ENTERPRISE_BASE_IMAGE}',
            f'AUTOMATION_IMAGE={AUTOMATION_BASE_IMAGE}',
            f'MARS_APP_IMAGE={lock["local_images"]["app"]}',
            f'MARS_AUTOMATION_IMAGE={lock["local_images"]["automation"]}',
            f'DO_MARS_CONFIG_ID={site["DO_MARS_CONFIG_ID"]}',
            f'DO_MARS_GATEWAY_URL={site["RUNTIME_ORIGIN"]}',
            f'DO_MARS_CALLBACK_URL={origin}',
            f'DO_MARS_APP_ORIGIN={origin}',
            f'AUTOMATION_SANDBOX_OPENHANDS_API_URL={origin}',
            'DO_MARS_INTERNAL_URL=http://mars-gateway:8100',
            f'MARS_GATEWAY_HOST_PORT={site["MARS_GATEWAY_HOST_PORT"]}',
            f'MARS_SECRETS_DIR={self.secrets_dir}',
            f'MARS_MAX_SANDBOXES={MAX_SANDBOXES}',
        ]
        lines += [f'{k}={site[k]}' for k in SITE_OPTIONAL if k in site]
        return '\n'.join(lines) + '\n'

    def write_derived(self, lock: dict, state: dict, site: dict[str, str]) -> None:
        data = self.render_derived(lock, state, site).encode()
        if self.compose_env.is_symlink():
            raise SetupError('compose.env is a symlink; refusing')
        if self.compose_env.exists():
            if sha256_file(self.compose_env) != state.get('derived_sha256'):
                raise SetupError('private/compose.env was edited outside the installer; remove it or restore it')
            if self.compose_env.read_bytes() == data:
                return
        atomic_write(self.compose_env, data, 0o600)
        state['derived_sha256'] = sha256_bytes(data)
        self.save_state(state)

    def config_files(self, stage: str, site: dict[str, str]) -> list[Path]:
        files = [self.stock] + ([self.mars_overlay] if stage == 'mars' else []) + [self.site_overlay]
        # Opt-in: layering the canvas-pilot overlay only when CANVAS_PILOT_BUILD
        # is set (docs/CANVAS.md) keeps every install that never sets it
        # byte-for-byte what it was before this setting existed.
        if stage == 'mars' and site.get('CANVAS_PILOT_BUILD'):
            files.append(self.canvas_overlay)
        return files

    def compose_args(self, state: dict, stage: str, site: dict[str, str]) -> list[str]:
        args = ['compose', '-p', state['project'], '--project-directory', str(self.compose_dir)]
        for f in self.config_files(stage, site):
            args += ['-f', str(f)]
        args += ['--env-file', str(self.stack_env), '--env-file', str(self.compose_env)]
        # mars-pool-reconciler carries `profiles: ['pool']` in docker-compose.mars.yml
        # precisely so an operator who never sets DO_MARS_POOL_ENABLED never gets
        # an extra running container; only activate the profile once they opted in.
        if stage == 'mars' and site.get('DO_MARS_POOL_ENABLED') == 'true':
            args += ['--profile', 'pool']
        return args

    def owned_config_files(self) -> set[str]:
        return {str(self.stock), str(self.site_overlay), str(self.mars_overlay), str(self.canvas_overlay)}


# ------------------------------------------------------------ init

def site_env_text(project: str, app_origin: str, runtime_origin: str, config_id: str,
                  app_port: int, gateway_port: int) -> str:
    return (
        f'# Site settings for Compose project {project}. Not secret. Edit, then run check.\n'
        '# Public HTTPS origins served by YOUR TLS terminator (two different hostnames).\n'
        f'APP_ORIGIN={app_origin}\n'
        f'RUNTIME_ORIGIN={runtime_origin}\n'
        '# UUID of the MARS environment config you created (needed only for --stage mars).\n'
        f'DO_MARS_CONFIG_ID={config_id}\n'
        '# Host loopback ports for your TLS terminator upstreams.\n'
        f'APP_PORT={app_port}\n'
        f'MARS_GATEWAY_HOST_PORT={gateway_port}\n'
        '# Optional (uncomment to change):\n'
        '# ENABLE_POSTHOG_ANALYTICS=false\n'
        '# POSTHOG_CLIENT_KEY=\n'
        '# DO_MARS_SESSION_TTL_SECONDS=28800\n'
        '# AUTOMATION_RUN_KEY_TTL_SECONDS=3600\n'
        '# OH_SANDBOX_LIFECYCLE_IDLE_SECONDS=1800\n'
        '# Opt-in warm pool (docs/INSTALL.md "Warm pool"); disabled by default,\n'
        '# every value below inert unless DO_MARS_POOL_ENABLED=true:\n'
        '# DO_MARS_POOL_ENABLED=false\n'
        '# DO_MARS_POOL_SANDBOX_SPEC_ID=\n'
        '# DO_MARS_POOL_MIN_HOT=0\n'
        '# DO_MARS_POOL_MIN_PAUSED_RESERVE=0\n'
        '# DO_MARS_POOL_MAX_TOTAL=0\n'
        '# DO_MARS_POOL_RECONCILE_INTERVAL_SECONDS=30\n'
        '# Opt-in pinned Canvas pilot build (docs/CANVAS.md); absolute path to an\n'
        '# `npm run build` output directory, unset leaves canvas-server unchanged:\n'
        '# CANVAS_PILOT_BUILD=\n'
        '# Optional MARS guest egress for LLM calls: the exact hostname(s) of the LLM\n'
        '# gateway/provider your resolved model profile calls (a provider connection can\n'
        '# override the default; do not guess from the model name). Plain comma-separated\n'
        '# lowercase hostnames, no https://, port, path or wildcard. Leave unset when\n'
        '# inference is served on the APP_ORIGIN hostname. Used by render-env-spec.\n'
        '# MARS_LLM_EGRESS_HOSTS=llm-gateway.example.invalid\n'
    )


def stack_env_text() -> str:
    lines = [
        '# SECRET. Generated once by mars_setup.py init; keep mode 0600; never commit or share.',
        '# Compose reads this as data. It is never sourced or executed.',
    ]
    lines += [f'{k}={secrets.token_urlsafe(32)}' for k in STACK_GENERATED]
    lines += [
        '# Optional, set by you if your agreement needs it:',
        'OPENHANDS_LICENSE_KEY=',
        '# Least-privilege gateway role: only AFTER the database reaches do_mars_001',
        '# (see docs/INSTALLER_CONTRACT.md). Leave unset for first activation.',
        '# MARS_GATEWAY_DB_USER=openhands_mars_gateway',
        '# MARS_GATEWAY_DB_PASS=',
    ]
    return '\n'.join(lines) + '\n'


def docker_project_conflict(install: Install, project: str, state: dict | None) -> str | None:
    """Return a reason if Docker already knows this project and it is not ours."""
    res = run_docker(['compose', 'ls', '--all', '--format', 'json'])
    if res.returncode != 0:
        raise SetupError('docker daemon not reachable (compose ls failed)')
    try:
        projects = parse_json_lines(res.stdout)
    except ValueError:
        raise SetupError('unexpected docker compose ls output') from None
    owned = install.owned_config_files()
    for entry in projects:
        if entry.get('Name') == project:
            files = {f for f in str(entry.get('ConfigFiles', '')).split(',') if f}
            if not files or not files <= owned:
                return f'Compose project {project} already exists with other Compose files'
    intent = (state or {}).get('compose_owned_intent') or {}
    started = bool(state and (state.get('baseline_up_at') or (
        intent.get('project') == project and set(intent.get('config_files', [])) <= owned)))
    vol = run_docker(['volume', 'ls', '--quiet', '--filter', f'label=com.docker.compose.project={project}'])
    if vol.returncode != 0:
        raise SetupError('docker daemon not reachable (volume ls failed)')
    if vol.stdout.strip() and not started:
        return f'Docker volumes for project {project} already exist and were not created by this install'
    return None


def cmd_init(args) -> int:
    lock = load_lock()
    if not PROJECT_RE.match(args.project):
        raise SetupError('--project must be lowercase letters, digits, - or _ (max 48, starting alphanumeric)')
    install = Install(args.dir)
    config_id = args.mars_config_id or ''
    site_text = site_env_text(args.project, args.app_origin, args.runtime_origin, config_id,
                              args.app_port, args.gateway_port)
    validate_site(parse_env_text(site_text, 'site.env', SITE_KEYS), 'baseline')

    state = None
    if install.dir.exists():
        if install.dir.is_symlink() or not install.dir.is_dir():
            raise SetupError('install path exists and is not a directory')
        if install.state_path.exists():
            state = install.load_state(lock)
            if state['project'] != args.project:
                raise SetupError(f'{install.dir} belongs to project {state["project"]}; use that name or a new directory')
        elif any(install.dir.iterdir()):
            raise SetupError(f'{install.dir} is not empty and was not created by this installer')

    try:
        conflict = docker_project_conflict(install, args.project, state)
    except SetupError as exc:
        print(f'warn  project collision check deferred to up: {exc}')
        conflict = None
    if conflict:
        raise SetupError(conflict + '; choose another --project (existing projects are never reused)')
    if not install.dir.exists():
        install.dir.mkdir(mode=0o700, parents=False)
        os.chmod(install.dir, 0o700)

    for sub, mode in ((install.private, 0o700), (install.compose_dir, 0o755), (install.secrets_dir, 0o700)):
        if sub.is_symlink():
            raise SetupError(f'{sub.name} is a symlink; refusing')
        if not sub.exists():
            sub.mkdir(mode=mode)
            os.chmod(sub, mode)

    report = []
    if install.site_env.exists():
        existing = install.read_site()
        wanted = parse_env_text(site_text, 'site.env', SITE_KEYS)
        for key in ('APP_ORIGIN', 'RUNTIME_ORIGIN', 'APP_PORT', 'MARS_GATEWAY_HOST_PORT'):
            if existing.get(key) != wanted[key]:
                raise SetupError(f'site.env already sets {key} differently; edit site.env instead of re-running init')
        if config_id and existing.get('DO_MARS_CONFIG_ID') not in ('', config_id):
            raise SetupError('site.env already sets DO_MARS_CONFIG_ID differently')
        report.append(('kept', install.site_env))
    else:
        create_exclusive(install.site_env, site_text.encode(), 0o600)
        report.append(('created', install.site_env))

    if install.stack_env.exists():
        install.read_stack()
        report.append(('kept', install.stack_env))
    else:
        create_exclusive(install.stack_env, stack_env_text().encode(), 0o600)
        report.append(('created', install.stack_env))

    for name in SECRET_FILES:
        path = install.secrets_dir / name
        if path.is_symlink():
            raise SetupError(f'secrets/{name} is a symlink; refusing')
        if path.exists():
            report.append(('kept', path))
            continue
        value = b'' if name == 'do_team_token' else secrets.token_urlsafe(48 if name == 'session_signing_key' else 32).encode()
        create_exclusive(path, value, 0o600)
        report.append(('created', path))

    for src, dest in (
        (SITE_OVERLAY_SRC, install.site_overlay),
        (MARS_OVERLAY_SRC, install.mars_overlay),
        (CANVAS_OVERLAY_SRC, install.canvas_overlay),
    ):
        report.append((write_if_absent_or_same(dest, src.read_bytes(), 0o644), dest))

    if state is None:
        state = {
            'schema': STATE_SCHEMA,
            'project': args.project,
            'install_dir': str(install.dir),
            'package_version': lock['package_version'],
            'enterprise_commit': ENTERPRISE_COMMIT,
            'created_at': utc_now(),
            'gateway_db_role': 'shared-application-role',
        }
    state['managed_files'] = sorted(str(p.relative_to(install.dir)) for _, p in report)
    install.save_state(state)
    for action, path in report:
        print(f'{action:8} {path}')
    print(f'\nInstall directory {install.dir} prepared for Compose project {args.project}. Nothing was started.')
    print('Next: fetch-compose, then check --stage baseline. Put the DO team token into')
    print(f'{install.secrets_dir}/do_team_token with your editor before --stage mars.')
    return 0


def cmd_fetch_compose(args) -> int:
    lock = load_lock()
    install = Install(args.dir)
    install.load_state(lock)
    if args.from_file:
        src = Path(args.from_file)
        if src.is_symlink() or not src.is_file() or src.stat().st_size > MAX_COMPOSE_BYTES:
            raise SetupError('--from-file must be a regular file within the size limit')
        data = src.read_bytes()
    else:
        data = download_compose()
    action = install_compose(install.stock, data)
    print(f'{action} {install.stock} (sha256 {COMPOSE_SHA256}, Enterprise {ENTERPRISE_VERSION} {ENTERPRISE_COMMIT})')
    return 0


def cmd_render_env_spec(args) -> int:
    lock = load_lock()
    install = Install(args.dir)
    install.load_state(lock)
    site = install.read_site()
    hosts = validate_site(site, 'baseline')
    try:
        init_key = (install.secrets_dir / 'init_api_key').read_text(encoding='utf-8').strip()
    except OSError:
        raise SetupError('cannot read secrets/init_api_key (render before changing its ownership)') from None
    if len(init_key) < 32 or not re.match(r'^[A-Za-z0-9_-]+$', init_key):
        raise SetupError('secrets/init_api_key must be 32+ URL-safe characters')
    llm_lines = ''.join(
        ['  # LLM gateway/provider hosts from MARS_LLM_EGRESS_HOSTS.\n']
        + [f'  - {host}\n' for host in hosts['llm_egress_hosts']]
    ) if hosts['llm_egress_hosts'] else ''
    text = ENV_SPEC_TEMPLATE.read_text(encoding='utf-8')
    for old, new in (
        ('  - app.example.invalid\n', f'  - {hosts["app_host"]}\n'),
        ('  - runtime.example.invalid\n', f'  - {hosts["runtime_host"]}\n' + llm_lines),
        ("'<same value as your init_api_key file>'", f"'{init_key}'"),
    ):
        if text.count(old) != 1:
            raise SetupError('templates/environment.example.yaml changed; refusing to render')
        text = text.replace(old, new)
    action = write_if_absent_or_same(install.env_spec, text.encode(), 0o600)
    print(f'{action} {install.env_spec} (mode 0600; contains the bootstrap secret, contents not shown)')
    print('Create the MARS config yourself with the official DO console/CLI/API; this tool makes no MARS API calls.')
    print('Then set DO_MARS_CONFIG_ID in site.env. Never commit, publish or share the rendered file.')
    return 0


# ------------------------------------------------------------ rendered config

def _env(services: dict, name: str) -> dict:
    env = services.get(name, {}).get('environment') or {}
    return {k: ('' if v is None else str(v)) for k, v in env.items()}


def verify_rendered(cfg: dict, stage: str, site: dict[str, str], stack: dict[str, str], lock: dict) -> list[str]:
    """Assert the merged Compose model. Returns failure messages naming keys only."""
    bad: list[str] = []
    services = cfg.get('services') or {}
    origin = site['APP_ORIGIN']
    host = validate_origin(origin, 'APP_ORIGIN')
    pool_enabled = stage == 'mars' and site.get('DO_MARS_POOL_ENABLED') == 'true'
    expected_services = (
        set(BASELINE_SERVICES)
        | ({'mars-gateway'} if stage == 'mars' else set())
        | ({'mars-pool-reconciler'} if pool_enabled else set())
    )
    if set(services) != expected_services:
        bad.append('merged services differ from the expected stock set'
                   + (' plus mars-gateway' if stage == 'mars' else '')
                   + (' plus mars-pool-reconciler' if pool_enabled else ''))

    def expect(cond: bool, msg: str) -> None:
        if not cond:
            bad.append(msg)

    for name, svc in services.items():
        for port in svc.get('ports') or []:
            expect(isinstance(port, dict) and port.get('host_ip') == '127.0.0.1',
                   f'{name} publishes a port on a non-loopback interface')
    proxy_ports = services.get('proxy', {}).get('ports') or []
    expect([str(p.get('published')) for p in proxy_ports] == [site['APP_PORT']], 'proxy is not published on APP_PORT')

    for name in ('app', 'worker'):
        env = _env(services, name)
        expect(env.get('OH_WEB_URL') == origin, f'{name} OH_WEB_URL is not APP_ORIGIN')
        expect(env.get('WEB_HOST') == host, f'{name} WEB_HOST is not the APP_ORIGIN host')
        expect(env.get('PERMITTED_CORS_ORIGINS') == origin, f'{name} PERMITTED_CORS_ORIGINS is not APP_ORIGIN')
        expect(env.get('DB_PASS') == stack['DB_PASS'], f'{name} DB_PASS is not the generated value')
        expect(env.get('AUTOMATIONS_SERVICE_KEY') == stack['AUTOMATION_SERVICE_KEY'],
               f'{name} AUTOMATIONS_SERVICE_KEY is not the generated value')
        expect(env.get('AUTOMATION_WEBHOOK_SECRET') == stack['AUTOMATION_WEBHOOK_SECRET'],
               f'{name} AUTOMATION_WEBHOOK_SECRET is not the generated value')
    expect(_env(services, 'db').get('POSTGRES_PASSWORD') == stack['DB_PASS'], 'db POSTGRES_PASSWORD is not the generated value')

    canvas_cmd = [str(c) for c in services.get('canvas-server', {}).get('command') or []]
    lock_idx = canvas_cmd.index('--lock-to-cloud') + 1 if '--lock-to-cloud' in canvas_cmd else -1
    expect(0 < lock_idx < len(canvas_cmd) and canvas_cmd[lock_idx] == origin, 'Canvas --lock-to-cloud is not APP_ORIGIN')

    auto = _env(services, 'automation-server')
    expect(auto.get('AUTOMATION_CORS_ORIGINS') == origin, 'automation AUTOMATION_CORS_ORIGINS is not APP_ORIGIN')
    expect(auto.get('AUTOMATION_OPENHANDS_API_BASE_URL') == 'http://app:3000', 'automation private API URL is not http://app:3000')
    for key in ('AUTOMATION_KV_SECRET', 'AUTOMATION_SERVICE_KEY'):
        expect(auto.get(key) == stack[key], f'automation {key} is not the generated value')

    images = {n: str(s.get('image', '')) for n, s in services.items()}
    if stage == 'baseline':
        expect(images.get('app') == ENTERPRISE_BASE_IMAGE and images.get('worker') == ENTERPRISE_BASE_IMAGE,
               'baseline app/worker image is not the pinned stock digest')
        expect(images.get('automation-server') == AUTOMATION_BASE_IMAGE,
               'baseline automation image is not the pinned stock digest')
        expect(auto.get('AUTOMATION_BASE_URL') == 'http://proxy', 'baseline AUTOMATION_BASE_URL is not the stock value')
        return bad

    app_tag, auto_tag = lock['local_images']['app'], lock['local_images']['automation']
    for name in ('app', 'worker', 'mars-gateway'):
        expect(images.get(name) == app_tag, f'{name} image is not the local MARS tag')
    expect(images.get('automation-server') == auto_tag, 'automation image is not the local MARS tag')
    expect(auto.get('AUTOMATION_BASE_URL') == origin, 'AUTOMATION_BASE_URL (native callback) is not the public APP_ORIGIN')
    expect(auto.get('AUTOMATION_SANDBOX_OPENHANDS_API_URL') == origin, 'guest API URL is not the public APP_ORIGIN')
    expect(auto.get('AUTOMATION_SANDBOX_INTERNAL_URL') == '', 'AUTOMATION_SANDBOX_INTERNAL_URL is not empty')
    pool_min_hot = site.get('DO_MARS_POOL_MIN_HOT', '0')
    pool_min_paused = site.get('DO_MARS_POOL_MIN_PAUSED_RESERVE', '0')
    pool_max_total = site.get('DO_MARS_POOL_MAX_TOTAL', '0')
    pool_spec_id = site.get('DO_MARS_POOL_SANDBOX_SPEC_ID', '')
    for name in ('app', 'worker'):
        env = _env(services, name)
        expect(env.get('OH_SANDBOX_MAX_NUM_SANDBOXES') == str(MAX_SANDBOXES), f'{name} sandbox cap is not {MAX_SANDBOXES}')
        expect(env.get('DO_MARS_CONFIG_ID') == site['DO_MARS_CONFIG_ID'], f'{name} DO_MARS_CONFIG_ID mismatch')
        expect(env.get('DO_MARS_GATEWAY_URL') == site['RUNTIME_ORIGIN'], f'{name} DO_MARS_GATEWAY_URL is not RUNTIME_ORIGIN')
        expect(env.get('DO_MARS_CALLBACK_URL') == origin, f'{name} DO_MARS_CALLBACK_URL is not APP_ORIGIN')
        vols = services.get(name, {}).get('volumes') or []
        expect(not any('docker.sock' in json.dumps(v) for v in vols), f'{name} still mounts the Docker socket')
        # Same site keys claim_one (do_mars_sandbox_service.py) reads at
        # construction time; a mismatch here means the pool and the
        # non-pool create path would disagree about enabled/spec/sizing.
        expect(env.get('DO_MARS_POOL_ENABLED') == ('true' if pool_enabled else 'false'),
               f'{name} DO_MARS_POOL_ENABLED does not match site.env')
        if pool_enabled:
            expect(env.get('DO_MARS_POOL_SANDBOX_SPEC_ID') == pool_spec_id,
                   f'{name} DO_MARS_POOL_SANDBOX_SPEC_ID mismatch')
            expect(env.get('DO_MARS_POOL_MIN_HOT') == pool_min_hot, f'{name} DO_MARS_POOL_MIN_HOT mismatch')
            expect(env.get('DO_MARS_POOL_MIN_PAUSED_RESERVE') == pool_min_paused,
                   f'{name} DO_MARS_POOL_MIN_PAUSED_RESERVE mismatch')
            expect(env.get('DO_MARS_POOL_MAX_TOTAL') == pool_max_total, f'{name} DO_MARS_POOL_MAX_TOTAL mismatch')
    if pool_enabled:
        pool = _env(services, 'mars-pool-reconciler')
        expect(pool.get('DO_MARS_POOL_ENABLED') == 'true', 'mars-pool-reconciler DO_MARS_POOL_ENABLED is not true')
        expect(pool.get('DO_MARS_CONFIG_ID') == site['DO_MARS_CONFIG_ID'], 'mars-pool-reconciler DO_MARS_CONFIG_ID mismatch')
        expect(pool.get('DO_MARS_POOL_SANDBOX_SPEC_ID') == pool_spec_id,
               'mars-pool-reconciler DO_MARS_POOL_SANDBOX_SPEC_ID mismatch')
        pool_ports = services.get('mars-pool-reconciler', {}).get('ports') or []
        expect(not pool_ports, 'mars-pool-reconciler publishes a port (it should expose none)')
    gw = _env(services, 'mars-gateway')
    expect(gw.get('DO_MARS_APP_ORIGIN') == origin, 'mars-gateway DO_MARS_APP_ORIGIN is not APP_ORIGIN')
    expect(gw.get('DB_USER') == (stack.get('MARS_GATEWAY_DB_USER') or 'openhands'), 'mars-gateway DB_USER is unexpected')
    gw_ports = services.get('mars-gateway', {}).get('ports') or []
    expect([str(p.get('published')) for p in gw_ports] == [site['MARS_GATEWAY_HOST_PORT']],
           'mars-gateway is not published on MARS_GATEWAY_HOST_PORT')

    canvas_build = site.get('CANVAS_PILOT_BUILD', '')
    if canvas_build:
        canvas_vols = services.get('canvas-server', {}).get('volumes') or []
        expect(
            any(
                isinstance(v, dict) and v.get('type') == 'bind' and v.get('source') == canvas_build
                and v.get('target') == '/opt/agent-canvas/frontend' and v.get('read_only') is True
                for v in canvas_vols
            ),
            'canvas-server is not bind-mounting CANVAS_PILOT_BUILD read-only at /opt/agent-canvas/frontend',
        )
    return bad


# ------------------------------------------------------------ checks

def gateway_can_read(st: os.stat_result, directory: bool) -> bool:
    need_u, need_g = (0o100, 0o010) if directory else (0o400, 0o040)
    return (st.st_uid == GATEWAY_UID and st.st_mode & need_u == need_u) or (
        st.st_gid == GATEWAY_GID and st.st_mode & need_g == need_g
    )


def secret_file_problems(secrets_dir: Path) -> list[str]:
    """stat() only; never reads secret contents."""
    try:
        return _secret_file_problems(secrets_dir)
    except PermissionError:
        return ['cannot stat the secret files as this user: keep the secrets directory owned by you '
                f'(chgrp {GATEWAY_GID} + chmod 0750), not root-owned']


def _secret_file_problems(secrets_dir: Path) -> list[str]:
    bad = []
    if secrets_dir.is_symlink() or not secrets_dir.is_dir():
        return ['secrets directory is missing or a symlink']
    dst = secrets_dir.stat()
    if dst.st_uid != os.geteuid():
        bad.append('secrets directory must stay owned by the installer user (only its group becomes '
                   f'{GATEWAY_GID})')
    if dst.st_mode & 0o007 & ~0o001:
        bad.append('secrets directory is world-readable or world-writable')
    if not gateway_can_read(dst, directory=True) and not dst.st_mode & 0o001:
        bad.append(f'secrets directory is not traversable by gid {GATEWAY_GID} '
                   f'(sudo chgrp {GATEWAY_GID} <dir>/secrets; chmod 0750 <dir>/secrets)')
    for name, (min_size, gateway) in SECRET_FILES.items():
        path = secrets_dir / name
        try:
            st = path.lstat()
        except FileNotFoundError:
            bad.append(f'secrets/{name} is missing or not a regular file')
            continue
        if not stat.S_ISREG(st.st_mode):
            bad.append(f'secrets/{name} is missing or not a regular file')
            continue
        if st.st_size < min_size:
            bad.append(f'secrets/{name} is empty or shorter than {min_size} bytes')
        if st.st_mode & 0o007:
            bad.append(f'secrets/{name} is world-accessible (mode {stat.S_IMODE(st.st_mode):04o})')
        if gateway and not gateway_can_read(st, directory=False):
            bad.append(f'secrets/{name} is not readable by gateway uid/gid {GATEWAY_UID} '
                       f'(sudo chown root:{GATEWAY_GID} and chmod 0640 on this file only)')
    return bad


def db_query(install: Install, state: dict, stage: str, sql: str, site: dict[str, str]) -> str | None:
    res = run_docker(install.compose_args(state, stage, site) + [
        'exec', '-T', 'db', 'psql', '-U', 'openhands', '-d', 'openhands', '-v', 'ON_ERROR_STOP=1', '-tA', '-c', sql,
    ], timeout=60)
    if res.returncode != 0:
        return None
    return res.stdout.strip()


def service_health(install: Install, state: dict, stage: str, site: dict[str, str]) -> dict[str, tuple[str, str]]:
    res = run_docker(install.compose_args(state, stage, site) + ['ps', '--all', '--format', 'json'])
    if res.returncode != 0:
        raise SetupError('docker compose ps failed (is the daemon reachable?)')
    out = {}
    for row in parse_json_lines(res.stdout):
        out[str(row.get('Service'))] = (str(row.get('State', '')), str(row.get('Health', '')))
    return out


class Report:
    def __init__(self):
        self.failures = 0

    def ok(self, msg: str) -> None:
        print(f'ok    {msg}')

    def warn(self, msg: str) -> None:
        print(f'warn  {msg}')

    def fail(self, msg: str) -> None:
        print(f'FAIL  {msg}')
        self.failures += 1

    def step(self, label: str, fn):
        try:
            result = fn()
        except SetupError as exc:
            self.fail(f'{label}: {exc}')
            return None
        self.ok(f'{label}: {result}' if isinstance(result, str) else label)
        return result if result is not None else True


def run_check(install: Install, stage: str) -> tuple[int, dict | None]:
    """Read-only readiness check. Only private/compose.env (derived, no secrets) may be rewritten."""
    r = Report()
    if sys.version_info < MIN_PYTHON:
        r.fail(f'Python >= {".".join(map(str, MIN_PYTHON))} required')
    lock = r.step('release lock matches immutable pins', load_lock)
    if not lock:
        return r.failures, None
    r.step('Dockerfiles pin the locked stock base digests', check_dockerfile_pins)
    r.step('package files match SHA256SUMS', lambda: f'{verify_package_sums()} files')
    state = r.step('installer state', lambda: install.load_state(lock))
    if not isinstance(state, dict):
        return r.failures, None
    for src, dest in (
        (SITE_OVERLAY_SRC, install.site_overlay),
        (MARS_OVERLAY_SRC, install.mars_overlay),
        (CANVAS_OVERLAY_SRC, install.canvas_overlay),
    ):
        if dest.is_symlink() or not dest.is_file() or dest.read_bytes() != src.read_bytes():
            r.fail(f'{dest.name} differs from this package')
    if install.stock.is_symlink() or not install.stock.is_file() or sha256_file(install.stock) != COMPOSE_SHA256:
        r.fail('compose/docker-compose.yml missing or not the pinned stock file (run fetch-compose)')
    else:
        r.ok(f'stock Compose {ENTERPRISE_VERSION} sha256 verified')
    loaded: dict[str, dict[str, str]] = {}
    r.step('private/stack.env (0600, generated secrets)', lambda: loaded.update(stack=install.read_stack()))
    r.step('site.env', lambda: loaded.update(site=install.read_site()))
    if 'site' in loaded and not r.step(f'site settings valid for {stage}',
                                       lambda: validate_site(loaded['site'], stage) and None):
        loaded.pop('site')
    if r.failures or 'stack' not in loaded or 'site' not in loaded:
        return r.failures, state
    stack, site = loaded['stack'], loaded['site']
    r.step('derived private/compose.env', lambda: install.write_derived(lock, state, site))
    if r.failures:
        return r.failures, state

    try:
        ver = compose_version()
        if ver < MIN_COMPOSE:
            r.fail(f'docker compose >= {".".join(map(str, MIN_COMPOSE))} required (found {".".join(map(str, ver))})')
        else:
            r.ok(f'docker compose {".".join(map(str, ver))}')
    except SetupError as exc:
        r.fail(str(exc))
        return r.failures, state
    res = run_docker(install.compose_args(state, stage, site) + ['config', '--format', 'json'])
    if res.returncode != 0:
        r.fail('compose merge failed (output withheld because it may contain secrets; '
               'run compose-command and inspect privately)')
        return r.failures, state
    try:
        cfg = json.loads(res.stdout)
    except ValueError:
        r.fail('compose config produced unexpected output')
        return r.failures, state
    problems = verify_rendered(cfg, stage, site, stack, lock)
    for p in problems:
        r.fail(p)
    if not problems:
        r.ok('rendered model: loopback ports, public WEB_URL/CORS/Canvas lock, private http://app:3000, generated secrets'
             + (', public callback + guest API, cap 10, no Docker socket' if stage == 'mars' else ''))

    try:
        conflict = docker_project_conflict(install, state['project'], state)
        if conflict:
            r.fail(conflict)
        else:
            r.ok(f'Compose project {state["project"]} is unused or owned by this install')
    except SetupError as exc:
        r.fail(str(exc))
        return r.failures, state

    if stage == 'mars':
        for p in secret_file_problems(install.secrets_dir):
            r.fail(p)
        built = state.get('built_images') or {}
        for key in ('app', 'automation'):
            tag = lock['local_images'][key]
            res = run_docker(['image', 'inspect', '--format', '{{.Id}}', tag])
            recorded = built.get(tag, '')
            if not re.match(r'^sha256:[0-9a-f]{64}$', recorded):
                r.fail(f'no build of {tag} is recorded for this install (run build)')
            elif res.returncode != 0:
                r.fail(f'local image {tag} not found (run build)')
            elif res.stdout.strip() != recorded:
                r.fail(f'local tag {tag} now points to a different image than this install built (run build again)')
            else:
                r.ok(f'local image {tag} is the image built for this install')
        try:
            health = service_health(install, state, 'baseline', site)
        except SetupError as exc:
            r.fail(str(exc))
            health = {}
        for svc in BASELINE_SERVICES:
            st, hl = health.get(svc, ('missing', ''))
            if st != 'running' or (svc in HEALTHCHECKED and hl != 'healthy'):
                r.fail(f'baseline service {svc} is {st}{"/" + hl if hl else ""} (start and verify the stock baseline first)')
        if health and all(health.get(s, ('',))[0] == 'running' for s in BASELINE_SERVICES):
            r.ok('baseline services running and healthy')
        rev = db_query(install, state, 'baseline', 'select version_num from alembic_version', site)
        if rev is None or not all(REVISION_RE.match(x) for x in rev.split('\n')) or '\n' in rev or not rev:
            r.fail('could not read a single alembic_version from the db service')
        elif rev not in (MIGRATION_BASE, MIGRATION_PACKAGE_PREV, MIGRATION_PACKAGE):
            r.fail(f'database revision {rev} is unsupported; this package needs {MIGRATION_BASE}, '
                   f'{MIGRATION_PACKAGE_PREV} or {MIGRATION_PACKAGE} and will not stamp, downgrade or overwrite it')
        else:
            if rev == MIGRATION_BASE:
                note = f' ({MIGRATION_PACKAGE_PREV} then {MIGRATION_PACKAGE} are applied by the MARS app at start)'
            elif rev == MIGRATION_PACKAGE_PREV:
                note = f' (upgrading from a prior install; {MIGRATION_PACKAGE} is applied by the MARS app at start)'
            else:
                note = ''
            r.ok(f'database revision {rev}{note}')
            state['last_db_revision'] = rev
        role = stack.get('MARS_GATEWAY_DB_USER', '')
        if not role:
            r.warn(f'mars-gateway uses the shared application DB role (evaluation default); '
                   f'see INSTALLER_CONTRACT.md for the least-privilege follow-up after {MIGRATION_PACKAGE_PREV}')
        elif rev not in (MIGRATION_PACKAGE_PREV, MIGRATION_PACKAGE):
            # The gateway's role check below reads columns do_mars_001 added
            # to v1_remote_sandbox; do_mars_002 only adds the unrelated pool
            # table, so either package revision has what this check needs.
            r.fail(f'MARS_GATEWAY_DB_USER is set but the database is not at {MIGRATION_PACKAGE_PREV} or '
                   f'{MIGRATION_PACKAGE} yet; leave it unset for the first activation')
        else:
            q = ("select has_column_privilege('{r}','public.v1_remote_sandbox','session_api_key_hash','SELECT')::int"
                 "||has_column_privilege('{r}','public.v1_remote_sandbox','session_api_key','SELECT')::int"
                 "||has_table_privilege('{r}','public.v1_remote_sandbox','UPDATE')::int").format(r=role)
            got = db_query(install, state, 'baseline', q, site)
            if got != '100':
                r.fail(f'gateway role {role} missing or not least-privilege (needs column SELECT only)')
            else:
                r.ok(f'gateway role {role} has column SELECT only')
    return r.failures, state


def cmd_check(args) -> int:
    install = Install(args.dir)
    failures, _ = run_check(install, args.stage)
    if failures:
        print(f'NOT READY ({failures} failure(s)) for stage {args.stage}')
        return 1
    if args.stage == 'baseline':
        print('READY baseline: configuration is consistent and can be started with `up --baseline`.')
    else:
        print('READY mars-activation: prerequisites hold; activation still needs your explicit `up --mars` attestation.')
    print('This check validates configuration and local state only; it does not prove an end-to-end install.')
    return 0


# ------------------------------------------------------------ actions

def cmd_build(args) -> int:
    lock = load_lock()
    install = Install(args.dir)
    state = install.load_state(lock)
    check_dockerfile_pins()
    verify_package_sums()
    if compose_version() < MIN_COMPOSE:
        raise SetupError('docker compose too old')
    images = {}
    for key, dockerfile in (('app', 'enterprise.Dockerfile'), ('automation', 'automation.Dockerfile')):
        tag = lock['local_images'][key]
        print(f'building {tag} from docker/{dockerfile} (stock base pinned by digest)', flush=True)
        res = run_docker(['build', '-f', str(PACKAGE_ROOT / 'docker' / dockerfile), '-t', tag, str(PACKAGE_ROOT)],
                         capture=False, timeout=7200)
        if res.returncode != 0:
            raise SetupError(f'docker build for {tag} failed; nothing was tagged by this step')
        inspect = run_docker(['image', 'inspect', '--format', '{{.Id}}', tag])
        image_id = inspect.stdout.strip() if inspect.returncode == 0 else ''
        if not re.match(r'^sha256:[0-9a-f]{64}$', image_id):
            state.pop('built_images', None)
            install.save_state(state)
            raise SetupError(f'could not record the image ID of {tag}; build state cleared, re-run build')
        images[tag] = image_id
    state['built_images'] = images
    state['built_at'] = utc_now()
    install.save_state(state)
    for tag, image_id in images.items():
        print(f'built {tag} {image_id}')
    print('Images are local only; this tool never pushes them.')
    return 0


def compose_up(install: Install, state: dict, stage: str, site: dict[str, str]) -> None:
    res = run_docker(install.compose_args(state, stage, site) + ['up', '-d', '--no-build'], timeout=7200)
    output = redact((res.stdout or '') + (res.stderr or ''), install.secret_values())
    print(output.rstrip())
    if res.returncode != 0:
        raise SetupError(f'docker compose up failed for stage {stage}; nothing was removed or reset')


def refuse_stock_after_mars(install: Install, state: dict, site: dict[str, str]) -> None:
    """Stock 1.88.0 cannot start on do_mars_001/do_mars_002 (ROLLBACK.md); never switch back automatically."""
    if state.get('mars_attestation') or state.get('mars_up_at'):
        raise SetupError('MARS activation was started for this install; starting stock images now would run them '
                         f'against {MIGRATION_PACKAGE_PREV} or {MIGRATION_PACKAGE}. Follow docs/ROLLBACK.md '
                         'manually; nothing was started')
    if not state.get('baseline_up_at'):
        return
    rev = db_query(install, state, 'baseline', 'select version_num from alembic_version', site)
    if rev != MIGRATION_BASE:
        raise SetupError(f'database revision is {rev if rev and REVISION_RE.match(rev) else "unreadable"}, '
                         f'not {MIGRATION_BASE}; refusing to (re)start stock images. Nothing was started')


def cmd_up(args) -> int:
    install = Install(args.dir)
    stage = 'mars' if args.mars else 'baseline'
    if stage == 'mars' and not (args.baseline_verified and args.backup_confirmed):
        raise SetupError('MARS activation needs --baseline-verified (stock stack healthy and a first normal '
                         'conversation verified by you) and --backup-confirmed (Enterprise and Automation '
                         'database dumps taken by you)')
    if stage == 'baseline' and (args.baseline_verified or args.backup_confirmed):
        raise SetupError('--baseline-verified/--backup-confirmed apply only to --mars')
    failures, state = run_check(install, stage)
    if failures or state is None:
        raise SetupError(f'check --stage {stage} failed; nothing was started')
    # run_check already validated site.env moments ago; re-read it (cheap,
    # read-only) rather than widen run_check's return value for this one use.
    site = install.read_site()
    if stage == 'baseline':
        refuse_stock_after_mars(install, state, site)
    state['compose_owned_intent'] = {
        'at': utc_now(), 'project': state['project'],
        'config_files': [str(f) for f in install.config_files(stage, site)],
    }
    install.save_state(state)
    if stage == 'mars':
        state['mars_attestation'] = {
            'at': utc_now(),
            'baseline_verified_by_operator': True,
            'backup_confirmed_by_operator': True,
            'db_revision_before': state.get('last_db_revision'),
        }
        install.save_state(state)
    compose_up(install, state, stage, site)
    state[f'{stage}_up_at'] = utc_now()
    install.save_state(state)
    if stage == 'baseline':
        print('\nStock baseline started (Docker sandboxes, stock images by digest). Next, yourself:')
        print('  1. wait until `status` shows app/automation healthy; route APP_ORIGIN to 127.0.0.1:APP_PORT via TLS')
        print('  2. sign in, configure an LLM, and complete one normal conversation')
        print('  3. back up the openhands and automations databases')
        print('  4. build, render-env-spec, create the MARS config, fill secrets, check --stage mars')
    else:
        print(f'\nMARS overlay started. Run `status` until alembic_version is {MIGRATION_PACKAGE} and services '
              'are healthy,')
        print('route RUNTIME_ORIGIN to 127.0.0.1:MARS_GATEWAY_HOST_PORT, then follow docs/INSTALL.md step 7.')
    return 0


def cmd_status(args) -> int:
    lock = load_lock()
    install = Install(args.dir)
    state = install.load_state(lock)
    site = install.read_site()
    stage = 'mars' if state.get('mars_up_at') else 'baseline'
    if not install.compose_env.exists():
        raise SetupError('private/compose.env missing; run check first')
    print(f'project {state["project"]}  stage {stage}  dir {install.dir}')
    for svc, (st, hl) in sorted(service_health(install, state, stage, site).items()):
        print(f'{svc:18} {st}{" (" + hl + ")" if hl else ""}')
    rev = db_query(install, state, stage, 'select version_num from alembic_version', site)
    print(f'alembic_version   {rev if rev and REVISION_RE.match(rev) else "unavailable"}')
    return 0


def cmd_compose_command(args) -> int:
    lock = load_lock()
    install = Install(args.dir)
    state = install.load_state(lock)
    site = install.read_site()
    print(' '.join(['docker'] + install.compose_args(state, args.stage, site)))
    print('# Paths only. Add ps/logs/exec yourself; `config` output contains secrets: never paste or publish it.')
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog='mars_setup.py', description=__doc__.split('\n\n')[0])
    sub = p.add_subparsers(dest='command', required=True)

    def with_dir(name: str, help_text: str):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument('--dir', required=True, help='install directory outside the package checkout')
        return sp

    sp = with_dir('init', 'prepare a new install directory (local only, starts nothing)')
    sp.add_argument('--project', required=True, help='new, unused Docker Compose project name')
    sp.add_argument('--app-origin', required=True, help='public https:// app origin')
    sp.add_argument('--runtime-origin', required=True, help='public https:// runtime gateway origin')
    sp.add_argument('--mars-config-id', default='', help='MARS environment config UUID (optional now)')
    sp.add_argument('--app-port', type=int, default=9043)
    sp.add_argument('--gateway-port', type=int, default=8100)
    sp.set_defaults(func=cmd_init)
    sp = with_dir('fetch-compose', 'download (or verify --from-file) the pinned stock docker-compose.yml')
    sp.add_argument('--from-file', help='offline copy to verify instead of downloading')
    sp.set_defaults(func=cmd_fetch_compose)
    sp = with_dir('check', 'read-only readiness check')
    sp.add_argument('--stage', choices=('baseline', 'mars'), required=True)
    sp.set_defaults(func=cmd_check)
    with_dir('build', 'build the two local MARS images from the pinned stock digests').set_defaults(func=cmd_build)
    with_dir('render-env-spec', 'render the secret-bearing MARS environment spec locally').set_defaults(
        func=cmd_render_env_spec)
    sp = with_dir('up', 'explicitly start the stock baseline or activate MARS')
    mode = sp.add_mutually_exclusive_group(required=True)
    mode.add_argument('--baseline', action='store_true')
    mode.add_argument('--mars', action='store_true')
    sp.add_argument('--baseline-verified', action='store_true')
    sp.add_argument('--backup-confirmed', action='store_true')
    sp.set_defaults(func=cmd_up)
    with_dir('status', 'service states and alembic revision (read-only)').set_defaults(func=cmd_status)
    sp = with_dir('compose-command', 'print the exact docker compose prefix for this install')
    sp.add_argument('--stage', choices=('baseline', 'mars'), required=True)
    sp.set_defaults(func=cmd_compose_command)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if sys.version_info < MIN_PYTHON:
        print(f'error: Python >= {".".join(map(str, MIN_PYTHON))} required', file=sys.stderr)
        return 2
    try:
        return args.func(args)
    except SetupError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
