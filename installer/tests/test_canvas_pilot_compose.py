"""Validates compose/docker-compose.canvas-pilot.yml with a real Docker CLI.

This package does not ship the stock Enterprise docker-compose.yml (see
MARS_STOCK_COMPOSE in test_mars_setup.py), so canvas-server here is a
synthetic stand-in for it: this test proves the overlay's own YAML, its
--lock-to-cloud substitution and its bind mount resolve exactly as written
through a real `docker compose config`, not that the stock image's actual
CLI accepts these flags end-to-end (see docs/CANVAS.md).
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PKG = Path(__file__).resolve().parents[2]
OVERLAY = PKG / 'compose' / 'docker-compose.canvas-pilot.yml'
REAL_DOCKER = shutil.which('docker')

STOCK_STUB = """\
services:
  canvas-server:
    image: stub/canvas-server:1.35.0
    command: [--port, '8000', --host, 0.0.0.0, --dir, /opt/agent-canvas/frontend,
              --base-path, /canvas, --disable-telemetry]
"""

# A minimal, isolated env: the real PATH/HOME for the docker CLI itself, plus
# only the variables this test or the overlay need. Never inherits the
# caller's full environment (secrets, unrelated DOCKER_* context).
_ENV_PASSTHROUGH = ('PATH', 'HOME', 'DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_CONFIG')


def _env(**extra):
    import os
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
    env.update(extra)
    return env


def _compose_config(tmp: Path, env: dict) -> subprocess.CompletedProcess:
    stock = tmp / 'stock.yml'
    stock.write_text(STOCK_STUB)
    return subprocess.run(
        ['docker', 'compose', '-f', str(stock), '-f', str(OVERLAY), 'config', '--format', 'json'],
        cwd=tmp, env=env, capture_output=True, text=True, timeout=30, check=False,
    )


@unittest.skipUnless(REAL_DOCKER, 'docker CLI not available')
class CanvasPilotCompose(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_overlay_binds_lock_to_cloud_to_web_url_and_mounts_the_build(self):
        build_dir = self.tmp / 'build'
        build_dir.mkdir()
        res = _compose_config(self.tmp, _env(
            WEB_URL='https://app.example.test',
            CANVAS_PILOT_BUILD=str(build_dir),
        ))
        self.assertEqual(res.returncode, 0, res.stderr)
        cfg = json.loads(res.stdout)
        svc = cfg['services']['canvas-server']

        cmd = [str(c) for c in svc['command']]
        self.assertIn('--lock-to-cloud', cmd)
        self.assertEqual(cmd[cmd.index('--lock-to-cloud') + 1], 'https://app.example.test')
        # The overlay fully replaces the stock command array (Compose's
        # default merge semantics for `command`); it must not accidentally
        # retain a stock --lock-to-cloud left over from an earlier layer.
        self.assertEqual(cmd.count('--lock-to-cloud'), 1)

        volumes = svc['volumes']
        self.assertEqual(len(volumes), 1)
        self.assertEqual(volumes[0]['source'], str(build_dir))
        self.assertEqual(volumes[0]['target'], '/opt/agent-canvas/frontend')
        self.assertTrue(volumes[0]['read_only'])

        # The image itself is untouched: this overlay swaps the served
        # bundle, not the container.
        self.assertEqual(svc['image'], 'stub/canvas-server:1.35.0')

    def test_overlay_refuses_to_resolve_without_canvas_pilot_build(self):
        res = _compose_config(self.tmp, _env(WEB_URL='https://app.example.test'))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn('CANVAS_PILOT_BUILD', res.stderr)

    def test_overlay_refuses_to_resolve_without_web_url(self):
        res = _compose_config(self.tmp, _env(CANVAS_PILOT_BUILD=str(self.tmp)))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn('WEB_URL', res.stderr)


if __name__ == '__main__':
    unittest.main()
