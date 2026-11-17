"""Interactive start readiness check through the MARS gateway (real PostgreSQL).

The native conversation start calls the shared
``SandboxService.wait_for_sandbox_running`` with an httpx client, which polls
``/alive`` on the agent server before the conversation is created. The MARS
gateway authenticates every sandbox route, so the probe must carry the
sandbox session key. These tests drive that real shared path; only the HTTP
transport (or, end to end, the DO port-forward) is replaced.
"""

import contextlib
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from aiohttp import web
from pydantic import SecretStr

from openhands.app_server.errors import SandboxError
from openhands.app_server.sandbox.do_mars_client import DO_MARS_BACKEND, DoMarsSession
from openhands.app_server.sandbox.do_mars_credentials import issue_session_key
from openhands.app_server.sandbox.do_mars_proxy import app as gateway_app
from openhands.app_server.sandbox.do_mars_sandbox_service import DoMarsSandboxService
from openhands.app_server.sandbox.sandbox_models import (
    AGENT_SERVER,
    ExposedUrl,
    SandboxInfo,
    SandboxStatus,
)
from openhands.app_server.sandbox.sandbox_service import SandboxService
from openhands.app_server.sandbox.sandbox_store import (
    LifecycleState,
    StoredSandbox,
    hash_session_api_key,
)
from openhands.app_server.user.specifiy_user_context import ADMIN

SIGNING_KEY = 'synthetic-unit-test-' + 'k' * 32
SID = 'sbx-health-auth'
PROVIDER_ID = '00000000-0000-4000-8000-000000000088'
GATEWAY = 'http://gateway.example.test'
ALIVE_URL = f'{GATEWAY}/{SID}/8000/alive'
DIAGNOSTICS_LOGGER = 'openhands.app_server.sandbox.do_mars_diagnostics'


@pytest.fixture(autouse=True)
def _env(monkeypatch, caplog):
    # Production disables propagation, so attach the capture handler directly
    # and cut propagation so source-tree logging config cannot capture twice.
    logger = logging.getLogger(DIAGNOSTICS_LOGGER)
    monkeypatch.setattr(logger, 'propagate', False)
    logger.addHandler(caplog.handler)
    monkeypatch.setenv('DO_MARS_SESSION_SIGNING_KEY', SIGNING_KEY)
    monkeypatch.setenv('DO_MARS_TEAM_TOKEN', 'synthetic-team-token')
    yield
    logger.removeHandler(caplog.handler)


async def _store(async_session_maker) -> str:
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
                lifecycle_state=LifecycleState.RUNNING,
                state_changed_at=now,
                last_active_at=now,
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


class FakeGuest:
    """httpx transport standing in for the gateway-authenticated agent server."""

    def __init__(self, expected_key: str, statuses: list[int] | None = None):
        self.expected_key = expected_key
        self.statuses = list(statuses or [])
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get('X-Session-API-Key') != self.expected_key:
            return httpx.Response(401, json={'detail': 'Invalid or missing key'})
        status = self.statuses.pop(0) if self.statuses else 200
        return httpx.Response(status, json={})


async def _wait(async_session_maker, guest: FakeGuest, timeout: int = 2):
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(guest)) as http,
        async_session_maker() as db,
    ):
        return await _service(db, http).wait_for_sandbox_running(
            SID, timeout=timeout, poll_interval=0, httpx_client=http
        )


@pytest.mark.asyncio
async def test_wait_for_running_sends_session_key_on_alive(async_session_maker):
    key = await _store(async_session_maker)
    guest = FakeGuest(key)
    info = await _wait(async_session_maker, guest)
    assert info.status == SandboxStatus.RUNNING
    assert guest.requests, 'readiness probe never ran'
    request = guest.requests[0]
    assert request.method == 'GET'
    assert str(request.url) == ALIVE_URL
    assert request.headers['X-Session-API-Key'] == key


@pytest.mark.asyncio
async def test_wait_for_running_polls_until_alive_succeeds(async_session_maker):
    key = await _store(async_session_maker)
    guest = FakeGuest(key, statuses=[503, 502])
    info = await _wait(async_session_maker, guest)
    assert info.status == SandboxStatus.RUNNING
    assert len(guest.requests) == 3


@pytest.mark.asyncio
async def test_non_success_health_never_reports_running(async_session_maker, caplog):
    key = await _store(async_session_maker)
    guest = FakeGuest(key, statuses=[503] * 10_000)
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger=DIAGNOSTICS_LOGGER)
    with pytest.raises(SandboxError):
        await _wait(async_session_maker, guest, timeout=1)
    assert guest.requests
    assert key not in caplog.text
    records = [r for r in caplog.records if r.msg == 'do_mars.health_probe_failed']
    assert records and {r.levelno for r in records} == {logging.DEBUG}
    assert records[0].__dict__['mars_status_code'] == 503


@pytest.mark.asyncio
async def test_wrong_session_key_is_refused_and_not_logged(async_session_maker, caplog):
    key = await _store(async_session_maker)
    guest = FakeGuest('a-different-session-key')
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger=DIAGNOSTICS_LOGGER)
    with pytest.raises(SandboxError):
        await _wait(async_session_maker, guest, timeout=1)
    assert guest.requests
    assert all(r.headers['X-Session-API-Key'] == key for r in guest.requests)
    assert key not in caplog.text
    records = [r for r in caplog.records if r.msg == 'do_mars.health_probe_failed']
    assert records and records[0].levelno == logging.WARNING
    assert records[0].__dict__['mars_status_code'] == 401
    assert records[0].__dict__['mars_operation'] == 'health.probe'
    assert GATEWAY not in records[0].getMessage()
    assert not any(
        k.startswith('mars_') and SID in str(v) for k, v in records[0].__dict__.items()
    )


@pytest.mark.asyncio
async def test_missing_session_key_sends_no_anonymous_probe(async_session_maker):
    await _store(async_session_maker)
    guest = FakeGuest('unused')
    sandbox = SandboxInfo(
        id=SID,
        created_by_user_id='owner',
        sandbox_spec_id='fixture',
        status=SandboxStatus.RUNNING,
        session_api_key=None,
        exposed_urls=[
            ExposedUrl(name=AGENT_SERVER, url=f'{GATEWAY}/{SID}/8000', port=8000)
        ],
    )
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(guest)) as http,
        async_session_maker() as db,
    ):
        alive = await _service(db, http)._check_agent_server_alive(sandbox, http)
    assert alive is False
    assert guest.requests == []


@pytest.mark.asyncio
async def test_transport_error_logs_class_only(async_session_maker, caplog):
    key = await _store(async_session_maker)

    def broken(request):
        raise httpx.ConnectError(f'boom {request.url} {key}')

    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger=DIAGNOSTICS_LOGGER)
    with pytest.raises(SandboxError):
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(broken)) as http,
            async_session_maker() as db,
        ):
            await _service(db, http).wait_for_sandbox_running(
                SID, timeout=1, poll_interval=0, httpx_client=http
            )
    records = [r for r in caplog.records if r.msg == 'do_mars.health_probe_failed']
    assert records and records[0].__dict__['mars_error_class'] == 'ConnectError'
    assert key not in caplog.text and 'boom' not in caplog.text


@pytest.mark.asyncio
async def test_base_service_probe_is_unchanged_for_other_providers():
    """Other providers keep the stock anonymous ``/alive`` probe."""
    seen: list[httpx.Request] = []

    def guest(request):
        seen.append(request)
        return httpx.Response(200)

    sandbox = SandboxInfo(
        id='sbx-other',
        created_by_user_id='owner',
        sandbox_spec_id='fixture',
        status=SandboxStatus.RUNNING,
        session_api_key='other-provider-key',
        exposed_urls=[
            ExposedUrl(name=AGENT_SERVER, url='http://agent.test', port=8000)
        ],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(guest)) as http:
        alive = await SandboxService._check_agent_server_alive(
            SimpleNamespace(_get_agent_server_url=lambda _: 'http://agent.test'),
            sandbox,
            http,
        )
    assert alive is True
    assert str(seen[0].url) == 'http://agent.test/alive'
    assert 'X-Session-API-Key' not in seen[0].headers


# End to end: the real gateway app over ASGI, the real row lookup and key
# check, and a local HTTP server standing in for the guest behind the DO
# port-forward.


@pytest.fixture
async def guest_server():
    seen: list[str] = []

    async def alive(request):
        seen.append(request.headers.get('X-Session-API-Key', ''))
        return web.json_response({})

    guest_app = web.Application()
    guest_app.router.add_get('/alive', alive)
    runner = web.AppRunner(guest_app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    @contextlib.asynccontextmanager
    async def forward(url, token):
        yield f'http://127.0.0.1:{port}'

    with patch('openhands.app_server.sandbox.do_mars_proxy.tcp_forward', forward):
        yield seen
    await runner.cleanup()


@pytest.mark.asyncio
async def test_gateway_refuses_anonymous_alive_so_stock_probe_cannot_pass(
    async_session_maker, guest_server
):
    """Root cause: the stock probe sends no key and the gateway refuses it."""
    await _store(async_session_maker)
    transport = httpx.ASGITransport(app=gateway_app)
    with patch('storage.database.a_session_maker', async_session_maker):
        async with (
            httpx.AsyncClient(transport=transport) as http,
            async_session_maker() as db,
        ):
            anonymous = await http.get(ALIVE_URL)
            service = _service(db, http)
            sandbox = await service.get_sandbox(SID)
            assert sandbox is not None
            stock = await SandboxService._check_agent_server_alive(
                service, sandbox, http
            )
    assert anonymous.status_code == 401
    assert stock is False
    assert guest_server == []


@pytest.mark.asyncio
async def test_native_start_wait_passes_through_real_gateway(
    async_session_maker, guest_server
):
    key = await _store(async_session_maker)
    transport = httpx.ASGITransport(app=gateway_app)
    with patch('storage.database.a_session_maker', async_session_maker):
        async with (
            httpx.AsyncClient(transport=transport) as http,
            async_session_maker() as db,
        ):
            info = await _service(db, http).wait_for_sandbox_running(
                SID, timeout=5, poll_interval=0, httpx_client=http
            )
    assert info.status == SandboxStatus.RUNNING
    assert guest_server == [key]
