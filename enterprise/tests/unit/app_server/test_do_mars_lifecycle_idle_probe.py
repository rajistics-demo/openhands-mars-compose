"""Lifecycle worker idle probe through the MARS gateway (real PostgreSQL).

``check_sandbox`` holds the sandbox row ``FOR UPDATE`` while it calls
``DoMarsSandboxService.get_idle_time``, which reaches Agent Server through the
public gateway. These tests run that real worker code, the real gateway app
(in-process over ASGI) and the real database lock together. Only the DO
port-forward is replaced, by a local HTTP server standing in for the guest.
"""

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from aiohttp import web
from pydantic import SecretStr

from openhands.app_server.sandbox.do_mars_client import DO_MARS_BACKEND, DoMarsSession
from openhands.app_server.sandbox.do_mars_credentials import issue_session_key
from openhands.app_server.sandbox.do_mars_proxy import app as gateway_app
from openhands.app_server.sandbox.do_mars_sandbox_service import DoMarsSandboxService
from openhands.app_server.sandbox.lifecycle.rules import Action
from openhands.app_server.sandbox.lifecycle.settings import SandboxLifecycleSettings
from openhands.app_server.sandbox.lifecycle.tasks import check_sandbox
from openhands.app_server.sandbox.sandbox_store import (
    LifecycleState,
    StoredSandbox,
    hash_session_api_key,
    lock_stored_sandbox_if_free,
)
from openhands.app_server.user.specifiy_user_context import ADMIN

SIGNING_KEY = 'synthetic-unit-test-' + 'k' * 32
SID = 'sbx-idle-probe'
PROVIDER_ID = '00000000-0000-4000-8000-000000000077'
GATEWAY = 'http://gateway.example.test'
GUEST_IDLE_SECONDS = 42.0


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv('DO_MARS_SESSION_SIGNING_KEY', SIGNING_KEY)
    monkeypatch.setenv('DO_MARS_TEAM_TOKEN', 'synthetic-team-token')


@pytest.fixture
async def guest():
    """Local stand-in for the guest Agent Server behind the DO port-forward."""
    seen: list[str] = []

    async def server_info(request):
        seen.append(request.headers.get('X-Session-API-Key', ''))
        return web.json_response({'idle_time': GUEST_IDLE_SECONDS})

    async def conversations(request):
        seen.append(request.headers.get('X-Session-API-Key', ''))
        return web.json_response({'items': []})

    guest_app = web.Application()
    guest_app.router.add_get('/server_info', server_info)
    guest_app.router.add_get('/api/conversations', conversations)
    runner = web.AppRunner(guest_app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    dials: list[str] = []

    @contextlib.asynccontextmanager
    async def forward(url, token):
        dials.append(url)
        yield f'http://127.0.0.1:{port}'

    with patch('openhands.app_server.sandbox.do_mars_proxy.tcp_forward', forward):
        yield seen, dials
    await runner.cleanup()


async def _store(async_session_maker, state=LifecycleState.RUNNING) -> str:
    key = issue_session_key(SID, PROVIDER_ID)
    now = datetime.now(UTC)
    async with async_session_maker() as db:
        db.add(
            StoredSandbox(
                id=SID,
                backend=DO_MARS_BACKEND,
                created_by_user_id='owner',
                sandbox_spec_id='fixture',
                session_api_key=SecretStr(key),
                session_api_key_hash=hash_session_api_key(key),
                provider_resource_id=PROVIDER_ID,
                provider_create_intent_at=now,
                created_at=now,
                lifecycle_state=state,
                state_changed_at=now,
                last_active_at=now - timedelta(minutes=5),
            )
        )
        await db.commit()
    return key


def _service(db, http) -> DoMarsSandboxService:
    provider = AsyncMock()
    provider.get_session.return_value = DoMarsSession(
        PROVIDER_ID, f'oh-{SID}', 'SESSION_STATUS_READY', 'fixture-config', None
    )
    return DoMarsSandboxService(
        user_context=ADMIN,
        db_session=db,
        httpx_client=http,
        max_num_sandboxes=10,
        sandbox_spec_service=AsyncMock(),
        do=provider,
        do_bearer_token='synthetic-team-token',
        gateway_base_url=GATEWAY,
        callback_url='https://app.example.test',
    )


@pytest.mark.asyncio
async def test_worker_idle_probe_succeeds_while_worker_holds_row_lock(
    async_session_maker, guest
):
    seen, dials = guest
    key = await _store(async_session_maker)
    probed: list[float | None] = []
    transport = httpx.ASGITransport(app=gateway_app)
    with patch('storage.database.a_session_maker', async_session_maker):
        async with (
            httpx.AsyncClient(transport=transport) as http,
            async_session_maker() as db,
        ):
            service = _service(db, http)
            real_probe = service.get_idle_time

            async def recording_probe(sandbox):
                result = await real_probe(sandbox)
                probed.append(result)
                return result

            with patch.object(service, 'get_idle_time', recording_probe):
                async with asyncio.timeout(15):
                    decision = await check_sandbox(
                        SID,
                        backend=DO_MARS_BACKEND,
                        settings=SandboxLifecycleSettings(
                            idle_seconds=3600,
                            max_session_seconds=86400,
                            delete_after_seconds=864000,
                        ),
                        db_session=db,
                        sandbox_service=service,
                        now=datetime.now(UTC),
                    )
            await db.rollback()
    assert probed == [GUEST_IDLE_SECONDS]
    assert dials and seen == [key]
    assert decision is not None and decision.action != Action.PAUSE


@pytest.mark.asyncio
async def test_native_request_is_served_while_worker_holds_row_lock(
    async_session_maker, guest
):
    seen, dials = guest
    key = await _store(async_session_maker)
    transport = httpx.ASGITransport(app=gateway_app)
    with patch('storage.database.a_session_maker', async_session_maker):
        async with (
            async_session_maker() as worker,
            httpx.AsyncClient(transport=transport, base_url=GATEWAY) as client,
        ):
            assert await lock_stored_sandbox_if_free(worker, DO_MARS_BACKEND, SID)
            async with asyncio.timeout(15):
                response = await client.get(
                    f'/{SID}/8000/api/conversations',
                    headers={'X-Session-API-Key': key},
                )
            await worker.rollback()
    assert response.status_code == 200
    assert response.json() == {'items': []}
    assert len(dials) == 1


@pytest.mark.asyncio
async def test_settled_paused_row_is_refused_without_dial_while_locked(
    async_session_maker, guest
):
    seen, dials = guest
    key = await _store(async_session_maker, LifecycleState.PAUSED)
    transport = httpx.ASGITransport(app=gateway_app)
    with patch('storage.database.a_session_maker', async_session_maker):
        async with (
            async_session_maker() as worker,
            httpx.AsyncClient(transport=transport, base_url=GATEWAY) as client,
        ):
            assert await lock_stored_sandbox_if_free(worker, DO_MARS_BACKEND, SID)
            async with asyncio.timeout(15):
                response = await client.get(
                    f'/{SID}/8000/server_info', headers={'X-Session-API-Key': key}
                )
            await worker.rollback()
    assert response.status_code == 409
    assert dials == [] and seen == []
