"""Clean-base portability tests for mars_apply.py.

Run: ENTERPRISE_188_TREE=<export of 1.88.0> AUTOMATION_130_TREE=<checkout of
1.30.0> python -m pytest scripts/test_mars_apply.py
The release trees are licensed inputs and are not shipped in this package.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
APPLY = PKG / 'scripts' / 'mars_apply.py'
TREES = {
    'enterprise': os.environ.get('ENTERPRISE_188_TREE'),
    'automation': os.environ.get('AUTOMATION_130_TREE'),
}


def _copy(component: str, tmp_path: Path) -> Path:
    src = TREES[component]
    if not src:
        pytest.skip(f'{component} release tree not provided')
    dst = tmp_path / component
    if component == 'enterprise':
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns('.git', '.venv'))
    else:
        # The image installs only the package into site-packages.
        shutil.copytree(Path(src) / 'openhands', dst / 'openhands')
    return dst


def _run(component: str, root: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(APPLY), str(PKG / component), str(root), *extra],
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize('component', ['enterprise', 'automation'])
def test_applies_to_release_and_refuses_second_apply(component, tmp_path):
    root = _copy(component, tmp_path)
    assert _run(component, root, '--check').returncode == 0
    first = _run(component, root)
    assert first.returncode == 0, first.stderr
    again = _run(component, root)
    assert again.returncode == 1
    assert 'refusing' in again.stderr


@pytest.mark.parametrize('component', ['enterprise', 'automation'])
def test_modified_base_file_writes_nothing(component, tmp_path):
    root = _copy(component, tmp_path)
    target = next(
        line.split(None, 1)[1].strip()
        for line in (PKG / component / 'BASE_SHA256').read_text().splitlines()
    )
    (root / target).write_text((root / target).read_text() + '\n# local edit\n')
    before = {p: p.read_bytes() for p in root.rglob('*.py')}
    result = _run(component, root)
    assert result.returncode == 1
    assert 'not the supported release version' in result.stderr
    assert {p: p.read_bytes() for p in root.rglob('*.py')} == before


def test_newer_enterprise_migration_head_is_refused(tmp_path):
    root = _copy('enterprise', tmp_path)
    (root / 'migrations/versions/214_upstream_next.py').write_text(
        "revision = '214'\ndown_revision = '213'\n"
    )
    result = _run('enterprise', root)
    assert result.returncode == 1
    assert 'migration head' in result.stderr
