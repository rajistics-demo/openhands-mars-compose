"""MARS package patches: guest API URL split, expiring run keys, task_outcome.

Each patch is opt-in; the defaults must keep Automation 1.30.0 behaviour.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from openhands.automation.backends.cloud import CloudSandboxBackend
from openhands.automation.config import clear_config_cache
from openhands.automation.models import Automation, AutomationRun, AutomationRunStatus
from openhands.automation.utils.api_key import get_api_key_for_automation_run


TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")
PRIVATE_API = "http://app.internal.example.invalid:3000"
GUEST_API = "https://app.example.invalid"


@pytest.fixture
def fresh_config(monkeypatch):
    def _set(**env):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        clear_config_cache()

    yield _set
    clear_config_cache()


async def _backend_env() -> dict[str, str]:
    backend = CloudSandboxBackend(api_url=PRIVATE_API, run=MagicMock())
    with patch(
        "openhands.automation.backends.cloud.get_api_key_for_automation_run",
        new_callable=AsyncMock,
        return_value="run-key",
    ):
        await backend.get_api_key()
    return backend.build_env_vars()


async def test_guest_api_url_defaults_to_private_service_url(fresh_config):
    fresh_config()
    env = await _backend_env()
    assert env["OPENHANDS_CLOUD_API_URL"] == PRIVATE_API


async def test_guest_api_url_override_applies_only_to_guest_env(fresh_config):
    fresh_config(AUTOMATION_SANDBOX_OPENHANDS_API_URL=GUEST_API)
    backend = CloudSandboxBackend(api_url=PRIVATE_API, run=MagicMock())
    with patch(
        "openhands.automation.backends.cloud.get_api_key_for_automation_run",
        new_callable=AsyncMock,
        return_value="run-key",
    ):
        await backend.get_api_key()
    assert backend.build_env_vars()["OPENHANDS_CLOUD_API_URL"] == GUEST_API
    # Service-side calls keep the private URL.
    assert backend.api_url == PRIVATE_API


def _run():
    return SimpleNamespace(
        id=uuid.uuid4(),
        automation=SimpleNamespace(user_id=uuid.uuid4(), org_id=uuid.uuid4()),
    )


def _capture_mint():
    seen: list[dict] = []
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"key": "sk-oh-run"})

    return seen, patch(
        "openhands.automation.utils.api_key.httpx.AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )


async def test_run_key_ttl_zero_keeps_legacy_shared_key_request(fresh_config):
    fresh_config()
    seen, serve = _capture_mint()
    with serve:
        await get_api_key_for_automation_run(_run())  # type: ignore[arg-type]
    assert seen == [{"name": "automation"}]


async def test_run_key_ttl_requests_unique_expiring_key(fresh_config):
    fresh_config(AUTOMATION_RUN_KEY_TTL_SECONDS="3600")
    run = _run()
    seen, serve = _capture_mint()
    before = datetime.now(UTC)
    with serve:
        await get_api_key_for_automation_run(run)  # type: ignore[arg-type]
        await get_api_key_for_automation_run(run)  # type: ignore[arg-type]
    after = datetime.now(UTC)
    assert [body["name"] for body in seen] == [f"automation-run-{run.id}"] * 2
    for body in seen:
        expiry = datetime.fromisoformat(body["expires_at"])
        assert expiry.tzinfo is not None
        assert (
            before + timedelta(seconds=3600)
            <= expiry
            <= after + timedelta(seconds=3600)
        )


@pytest.mark.parametrize("value", ["-1", "86401"])
def test_run_key_ttl_is_bounded_by_enterprise_limit(fresh_config, value):
    from openhands.automation.config import get_settings

    fresh_config(AUTOMATION_RUN_KEY_TTL_SECONDS=value)
    with pytest.raises(ValueError):
        get_settings()


async def _running_run(async_session, metadata=None):
    automation = Automation(
        user_id=TEST_USER_ID,
        org_id=TEST_ORG_ID,
        name="MARS native",
        trigger={"type": "cron", "schedule": "0 9 * * *", "timezone": "UTC"},
        tarball_path="s3://bucket/code.tar.gz",
        entrypoint="python3 main.py",
    )
    async_session.add(automation)
    await async_session.commit()
    run = AutomationRun(
        automation_id=automation.id,
        status=AutomationRunStatus.RUNNING,
        run_metadata=metadata,
    )
    async_session.add(run)
    await async_session.commit()
    return run


@pytest.mark.parametrize("status", ["COMPLETED", "FAILED"])
async def test_task_outcome_is_persisted_as_result_metadata(
    async_client, async_session, status
):
    run = await _running_run(async_session, {"existing": "value"})
    outcome = {"passed": status == "COMPLETED", "results": [{"verified": True}]}
    response = await async_client.post(
        f"/api/automation/v1/runs/{run.id}/complete",
        json={"status": status, "task_outcome": outcome},
    )
    assert response.status_code == 200
    await async_session.refresh(run)
    assert run.run_metadata == {"existing": "value", "task_outcome": outcome}
    assert run.status == AutomationRunStatus[status]
    if status == "COMPLETED":
        assert run.status_detail is None


async def test_callback_without_task_outcome_leaves_metadata_untouched(
    async_client, async_session
):
    run = await _running_run(async_session, None)
    response = await async_client.post(
        f"/api/automation/v1/runs/{run.id}/complete",
        json={"status": "COMPLETED"},
    )
    assert response.status_code == 200
    await async_session.refresh(run)
    assert run.run_metadata is None
