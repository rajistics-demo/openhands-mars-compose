"""do_mars_pool_worker.py: process-level entrypoint, not business logic
(that is do_mars_pool_reconciler.py, already covered by
test_do_mars_pool_reconciler.py). The one property worth a standalone test
here is the safety-relevant one: disabled-by-default must short-circuit
before ever importing anything Enterprise-core-only (storage.database),
since a misconfigured or accidentally-started worker process must not blow
up just from being invoked.
"""

import sys

import pytest

pytestmark = pytest.mark.asyncio


async def test_disabled_pool_returns_without_importing_enterprise_core(monkeypatch):
    monkeypatch.delenv('DO_MARS_POOL_ENABLED', raising=False)
    assert 'storage.database' not in sys.modules

    from openhands.app_server.sandbox.do_mars_pool_worker import run_forever

    await run_forever()

    # If the disabled branch had fallen through to the Enterprise-only
    # import, this module either would already be in sys.modules (if a
    # stub/real module exists) or the call above would have raised
    # ModuleNotFoundError first - this environment deliberately has no
    # storage.database stub, so either outcome would be a visible failure.
    assert 'storage.database' not in sys.modules


async def test_invalid_pool_config_from_env_exits_cleanly_not_a_traceback(monkeypatch):
    monkeypatch.setenv('DO_MARS_POOL_ENABLED', 'true')
    monkeypatch.delenv('DO_MARS_POOL_SANDBOX_SPEC_ID', raising=False)

    from openhands.app_server.sandbox.do_mars_pool_worker import run_forever

    with pytest.raises(SystemExit):
        await run_forever()


async def test_missing_do_mars_config_id_exits_before_importing_enterprise_core(
    monkeypatch,
):
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
    monkeypatch.setenv('DO_MARS_POOL_ENABLED', 'true')
    monkeypatch.setenv('DO_MARS_POOL_SANDBOX_SPEC_ID', 'fixture-spec')
    monkeypatch.setenv('DO_MARS_POOL_MIN_HOT', '1')
    monkeypatch.setenv('DO_MARS_POOL_MAX_TOTAL', '1')
    monkeypatch.delenv('DO_MARS_CONFIG_ID', raising=False)
    monkeypatch.delenv('DO_MARS_POOL_CONFIG_ID', raising=False)
    assert 'storage.database' not in sys.modules

    from openhands.app_server.sandbox.do_mars_pool_worker import run_forever

    with pytest.raises(SystemExit, match='DO_MARS_CONFIG_ID'):
        await run_forever()

    # Same safety property as the disabled-pool test above: a missing
    # required setting must fail before touching anything Enterprise-core.
    assert 'storage.database' not in sys.modules


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
