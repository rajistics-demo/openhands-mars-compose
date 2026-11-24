"""do_mars_pool_worker.py: process-level entrypoint, not business logic
(that is do_mars_pool_reconciler.py, already covered by
test_do_mars_pool_reconciler.py). The one property worth a standalone test
here is the safety-relevant one: disabled-by-default must short-circuit
before ever importing anything Enterprise-core-only (storage.database),
since a misconfigured or accidentally-started worker process must not blow
up just from being invoked.

The two "...without_importing_enterprise_core" tests below run that check
in a bare ``sys.executable`` subprocess, deliberately *not* an in-process
``sys.modules`` check. In this checkout, ``tests/unit/conftest.py`` -
loaded for every test under ``tests/unit/``, including this file, before
any test body runs - does ``from storage.api_key import ApiKey`` and many
siblings, which imports ``storage.database`` as a side effect. That makes
the property these two tests exist to assert untestable in-process here:
``'storage.database' not in sys.modules`` would be false before the
module under test is even imported, for a reason having nothing to do
with do_mars_pool_worker.py. A bare subprocess has none of that conftest
machinery, so it is the only place left in this checkout where
do_mars_pool_worker.py's own import behavior - not conftest's - can
actually be observed.
"""

import os
import subprocess
import sys
import types

import pytest

pytestmark = pytest.mark.asyncio

_CHECK_SNIPPET = """
import sys
assert 'storage.database' not in sys.modules
assert 'openhands.app_server.config' not in sys.modules
import asyncio
from openhands.app_server.sandbox.do_mars_pool_worker import run_forever
try:
    asyncio.run(run_forever())
except SystemExit as exc:
    print('SYSTEMEXIT:' + str(exc))
assert 'storage.database' not in sys.modules
assert 'openhands.app_server.config' not in sys.modules
print('NO-ENTERPRISE-IMPORT-OK')
"""


def _run_in_bare_subprocess(env_overrides: dict) -> subprocess.CompletedProcess:
    """Run ``_CHECK_SNIPPET`` in a fresh interpreter, not this test's own.

    Only the env vars under test are overridden; everything else from the
    current process (PYTHONPATH in particular, so the real
    ``openhands``/``do_mars_pool_worker`` package resolves the same way
    pytest.ini's own ``pythonpath = .`` does) is inherited unchanged.
    """
    env = dict(os.environ)
    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run(
        [sys.executable, '-c', _CHECK_SNIPPET],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


async def test_disabled_pool_returns_without_importing_enterprise_core():
    result = _run_in_bare_subprocess({'DO_MARS_POOL_ENABLED': None})

    assert result.returncode == 0, result.stdout + result.stderr
    assert 'NO-ENTERPRISE-IMPORT-OK' in result.stdout
    assert 'SYSTEMEXIT' not in result.stdout  # disabled pool must not exit(1)


async def test_invalid_pool_config_from_env_exits_cleanly_not_a_traceback(monkeypatch):
    monkeypatch.setenv('DO_MARS_POOL_ENABLED', 'true')
    monkeypatch.delenv('DO_MARS_POOL_SANDBOX_SPEC_ID', raising=False)

    from openhands.app_server.sandbox.do_mars_pool_worker import run_forever

    with pytest.raises(SystemExit):
        await run_forever()


async def test_missing_do_mars_config_id_exits_before_importing_enterprise_core():
    """The worker must bind pool inventory to the *provider's* config id
    (DO_MARS_CONFIG_ID - the same value do_mars_spec_service.py uses for
    every non-pool create_session call), never a separately-settable
    pool-only label. A prior revision read an independent
    DO_MARS_POOL_CONFIG_ID that silently defaulted to the literal string
    "default", which could diverge from the real provider config and let a
    stale-config VM be claimed after an operator rotated DO_MARS_CONFIG_ID.
    This asserts the fail-fast replacement: missing DO_MARS_CONFIG_ID exits
    cleanly instead of either falling back to a made-up default or crashing
    deep inside DoMarsPoolReconciler.
    """
    result = _run_in_bare_subprocess(
        {
            'DO_MARS_POOL_ENABLED': 'true',
            'DO_MARS_POOL_SANDBOX_SPEC_ID': 'fixture-spec',
            'DO_MARS_POOL_MIN_HOT': '1',
            'DO_MARS_POOL_MAX_TOTAL': '1',
            'DO_MARS_CONFIG_ID': None,
            'DO_MARS_POOL_CONFIG_ID': None,
        }
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert 'NO-ENTERPRISE-IMPORT-OK' in result.stdout
    assert 'SYSTEMEXIT:' in result.stdout
    assert 'DO_MARS_CONFIG_ID' in result.stdout


async def test_pool_config_id_env_var_no_longer_read(monkeypatch):
    """Regression guard for the fix itself: DO_MARS_POOL_CONFIG_ID must have
    no effect at all. If it is ever wired back in, this proves it can no
    longer substitute for the real DO_MARS_CONFIG_ID binding."""
    monkeypatch.setenv('DO_MARS_POOL_ENABLED', 'true')
    monkeypatch.setenv('DO_MARS_POOL_SANDBOX_SPEC_ID', 'fixture-spec')
    monkeypatch.setenv('DO_MARS_POOL_MIN_HOT', '1')
    monkeypatch.setenv('DO_MARS_POOL_MAX_TOTAL', '1')
    monkeypatch.delenv('DO_MARS_CONFIG_ID', raising=False)
    monkeypatch.setenv('DO_MARS_POOL_CONFIG_ID', 'some-other-value')

    from openhands.app_server.sandbox.do_mars_pool_worker import run_forever

    with pytest.raises(SystemExit, match='DO_MARS_CONFIG_ID'):
        await run_forever()


async def test_enabled_pool_resolves_session_factory_via_stock_db_session_path(
    monkeypatch,
):
    """Regression for the AWS runtime failure: ``storage.database.a_session_maker``
    is an ``@contextlib.asynccontextmanager`` that *yields* a session - calling it
    directly does not hand back an ``AsyncSession``, which is what
    ``DoMarsPoolReconciler.session_factory`` must return on every call (it is
    used with no ``async with``, see do_mars_pool_reconciler.py). The fix routes
    through the same stock ``get_global_config().db_session.get_async_session_maker()``
    path Enterprise itself uses to get a plain, repeatedly-callable sessionmaker,
    awaited once before the loop starts - never the asynccontextmanager object
    itself, and never re-resolved on every tick.
    """
    monkeypatch.setenv('DO_MARS_POOL_ENABLED', 'true')
    monkeypatch.setenv('DO_MARS_POOL_SANDBOX_SPEC_ID', 'fixture-spec')
    monkeypatch.setenv('DO_MARS_POOL_MIN_HOT', '1')
    monkeypatch.setenv('DO_MARS_POOL_MAX_TOTAL', '1')
    monkeypatch.setenv('DO_MARS_CONFIG_ID', '3f2a9c1e-5b6d-4e7f-8a9b-0c1d2e3f4a5b')
    monkeypatch.setenv('DO_MARS_TEAM_TOKEN', 'synthetic-team-token')

    captured = {}

    def real_sessionmaker_sentinel():
        """Stands in for the real plain, repeatedly-callable sessionmaker:
        if the worker ever passed the un-awaited coroutine, or the
        asynccontextmanager-wrapped db_session object itself, instead of
        this, the identity assertion below would fail."""
        raise AssertionError('must not actually be called in this test')

    class FakeDbSession:
        async def get_async_session_maker(self):
            return real_sessionmaker_sentinel

    class FakeGlobalConfig:
        db_session = FakeDbSession()

    fake_config_module = types.ModuleType('openhands.app_server.config')
    fake_config_module.get_global_config = lambda: FakeGlobalConfig()
    monkeypatch.setitem(sys.modules, 'openhands.app_server.config', fake_config_module)

    class _HaltLoop(BaseException):
        """Distinct from Exception: the worker's per-tick try/except only
        catches Exception, so this reaches the test unimpeded and ends the
        loop after exactly one tick, without needing real signal delivery."""

    class FakeReconciler:
        def __init__(self, *, session_factory, **kwargs):
            captured['session_factory'] = session_factory
            captured['kwargs'] = kwargs

        async def run_once(self):
            raise _HaltLoop

    from openhands.app_server.sandbox import do_mars_pool_reconciler as reconciler_mod

    monkeypatch.setattr(reconciler_mod, 'DoMarsPoolReconciler', FakeReconciler)

    from openhands.app_server.sandbox.do_mars_pool_worker import run_forever

    with pytest.raises(_HaltLoop):
        await run_forever()

    assert captured['session_factory'] is real_sessionmaker_sentinel
    assert captured['kwargs']['config_id'] == '3f2a9c1e-5b6d-4e7f-8a9b-0c1d2e3f4a5b'
