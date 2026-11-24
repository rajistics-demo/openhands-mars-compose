import pytest

from openhands.app_server.sandbox.do_mars_pool_config import (
    DoMarsPoolConfig,
    PoolConfigError,
)


def test_disabled_by_default(monkeypatch):
    for key in list(__import__('os').environ):
        if key.startswith('DO_MARS_POOL_'):
            monkeypatch.delenv(key, raising=False)
    config = DoMarsPoolConfig.from_env()
    assert config.enabled is False
    assert config.min_hot == 0
    assert config.min_paused_reserve == 0


def test_enabled_requires_sandbox_spec_id():
    with pytest.raises(PoolConfigError, match='SANDBOX_SPEC_ID'):
        DoMarsPoolConfig(enabled=True, sandbox_spec_id=None, max_total=1)


def test_max_total_must_cover_the_floor():
    with pytest.raises(PoolConfigError, match='MAX_TOTAL'):
        DoMarsPoolConfig(
            enabled=True,
            sandbox_spec_id='do-mars-agent-server',
            min_hot=2,
            min_paused_reserve=1,
            max_total=2,
        )


def test_valid_config_accepted():
    config = DoMarsPoolConfig(
        enabled=True,
        sandbox_spec_id='do-mars-agent-server',
        min_hot=2,
        min_paused_reserve=1,
        max_total=3,
    )
    assert config.max_total == 3


def test_replenish_can_be_disabled_independently_of_minimums(monkeypatch):
    monkeypatch.setenv('DO_MARS_POOL_ENABLED', 'true')
    monkeypatch.setenv('DO_MARS_POOL_SANDBOX_SPEC_ID', 'do-mars-agent-server')
    monkeypatch.setenv('DO_MARS_POOL_MIN_HOT', '3')
    monkeypatch.setenv('DO_MARS_POOL_REPLENISH_ENABLED', 'false')
    config = DoMarsPoolConfig.from_env()
    assert config.min_hot == 3
    assert config.replenish_enabled is False


def test_rejects_non_boolean(monkeypatch):
    monkeypatch.setenv('DO_MARS_POOL_ENABLED', 'yes')
    with pytest.raises(PoolConfigError):
        DoMarsPoolConfig.from_env()
