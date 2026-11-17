#!/usr/bin/env python3
"""Fake docker CLI for installer tests.

Installed as `docker` on PATH. Reads $HOME/fake_docker.json (the installer
passes only an allowlisted environment, so HOME is the channel) and appends
each call to $HOME/docker_calls.jsonl. `compose ... config` is forwarded to the
real Docker CLI when the scenario names one, so merges use real Compose.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

home = Path(os.environ['HOME'])
scenario = json.loads((home / 'fake_docker.json').read_text())
argv = sys.argv[1:]
with open(home / 'docker_calls.jsonl', 'a') as log:
    log.write(json.dumps({'argv': argv, 'env_keys': sorted(os.environ)}) + '\n')


def out(text='', rc=0):
    sys.stdout.write(text)
    sys.exit(rc)


if argv[:2] == ['compose', 'version']:
    out(scenario.get('compose_version', '2.29.1') + '\n')
if argv[:2] == ['compose', 'ls']:
    out(json.dumps(scenario.get('projects', [])), scenario.get('daemon_rc', 0))
if argv[:2] == ['volume', 'ls']:
    out(''.join(v + '\n' for v in scenario.get('volumes', [])), scenario.get('daemon_rc', 0))
if argv[:2] == ['image', 'inspect']:
    tag = argv[-1]
    if tag in scenario.get('images', []):
        out(scenario.get('image_ids', {}).get(tag, 'sha256:' + 'ab' * 32) + '\n')
    out('', 1)
if argv[:1] == ['build']:
    out('', scenario.get('build_rc', 0))
if argv[:1] == ['compose']:
    if 'config' in argv:
        real = scenario.get('real_docker')
        if not real:
            out('', 1)
        res = subprocess.run([real, *argv], capture_output=True, text=True, env=dict(os.environ))
        sys.stderr.write(res.stderr)
        out(res.stdout, res.returncode)
    if 'ps' in argv:
        out(''.join(json.dumps(r) + '\n' for r in scenario.get('ps', [])))
    if 'exec' in argv:
        sql = argv[-1]
        if 'alembic_version' in sql:
            rev = scenario.get('revision')
            out((rev + '\n') if rev else '', 0 if rev else 1)
        if 'has_column_privilege' in sql:
            out(scenario.get('role_priv', '100') + '\n')
        out('', 1)
    if 'up' in argv:
        out(scenario.get('up_output', ''), scenario.get('up_rc', 0))
out('unsupported fake docker call\n', 3)
