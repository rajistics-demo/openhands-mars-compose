"""MARS diagnostic logging carries status and error class, never secrets.

Each failure below is built so the raw exception text, URL, headers, provider
body and identifiers contain a marker string; the tests assert the expected
method/status/class fields are logged and that no marker reaches any
OpenHands log record.
"""

import asyncio
import contextlib
import logging
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from aiohttp import web
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.websockets import WebSocketDisconnect

from openhands.app_server.errors import SandboxDeleteRetryError, SandboxError
from openhands.app_server.sandbox.do_mars_client import DO_MARS_BACKEND, DoMarsClient
from openhands.app_server.sandbox.do_mars_credentials import issue_session_key
from openhands.app_server.sandbox.do_mars_diagnostics import log_diagnostic
from openhands.app_server.sandbox.do_mars_proxy import app as gateway_app
from openhands.app_server.sandbox.do_mars_sandbox_service import DoMarsSandboxService
from openhands.app_server.sandbox.do_mars_tunnel import (
    tcp_forward,
    tunnel_http_request,
)
from openhands.app_server.sandbox.sandbox_models import SandboxInfo, SandboxStatus
from openhands.app_server.sandbox.sandbox_store import (
    StoredSandbox,
    hash_session_api_key,
)
from openhands.app_server.user.specifiy_user_context import ADMIN

SIGNING_KEY = 'synthetic-unit-test-' + 'k' * 32
TEAM_TOKEN = 'dop_v1_SYNTHETICSECRETTEAMTOKEN'
SANDBOX_ID = 'sbxSECRETSANDBOXID'
PROVIDER_ID = '0000feed-0000-4000-8000-00000000beef'
BODY_MARKER = 'SECRETPROVIDERBODY'
GATEWAY = 'http://gateway.example.test'
DIAG = 'openhands.app_server.sandbox.do_mars_diagnostics'


@pytest.fixture(autouse=True)
def _env(monkeypatch, caplog):
    # The production logger may disable propagation; attach the capture handler
    # directly so this test observes the same records in source and image runs.
    logger = logging.getLogger(DIAG)
    logger.addHandler(caplog.handler)
    monkeypatch.setenv('DO_MARS_SESSION_SIGNING_KEY', SIGNING_KEY)
    monkeypatch.setenv('DO_MARS_TEAM_TOKEN', TEAM_TOKEN)
    yield
    logger.removeHandler(caplog.handler)


def _diag(caplog, event):
    return [r for r in caplog.records if r.name == DIAG and r.getMessage() == event]


def _fields(record) -> dict:
    return {k[5:]: v for k, v in vars(record).items() if k.startswith('mars_')}


def _assert_private(caplog, *markers: str):
    records = [r for r in caplog.records if r.name.startswith('openhands')]
    assert records
    for record in records:
        assert record.exc_info is None and record.exc_text is None
        rendered = (
            record.getMessage() + ' ' + ' '.join(str(v) for v in vars(record).values())
        )
        for marker in markers:
            assert marker not in rendered, (record.getMessage(), marker)


def _private_markers(*extra: str) -> tuple[str, ...]:
    return (
        TEAM_TOKEN,
        SANDBOX_ID,
        PROVIDER_ID,
        BODY_MARKER,
        'api.digitalocean',
        *extra,
    )


def test_diagnostic_fields_are_closed_vocabulary(caplog):
    caplog.set_level(logging.INFO, logger=DIAG)
    log_diagnostic(
        'do_mars.control_response',
        operation='https://evil.example.invalid/x',
        method='BREW',
        status_code=503,
        error_class='ConnectError: token=abc',
        provider_status=PROVIDER_ID,
        sandbox_id=SANDBOX_ID,
        reason=BODY_MARKER,
    )
    (record,) = _diag(caplog, 'do_mars.control_response')
    assert _fields(record) == {'status_code': 503, 'provider_status': 'unrecognized'}
    with pytest.raises(ValueError):
        log_diagnostic('free text with ' + TEAM_TOKEN)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['status', 'transport'])
async def test_control_plane_failure_logs_method_and_status_only(caplog, failure):
    caplog.set_level(logging.INFO)

    def handler(request: httpx.Request) -> httpx.Response:
        if failure == 'transport':
            raise httpx.ConnectError(
                f'cannot reach {request.url} with {TEAM_TOKEN}', request=request
            )
        return httpx.Response(503, text=f'{BODY_MARKER} {TEAM_TOKEN}')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(SandboxError):
            await DoMarsClient(TEAM_TOKEN, http).delete_session(PROVIDER_ID)
    if failure == 'status':
        (record,) = _diag(caplog, 'do_mars.control_response')
        assert _fields(record) == {
            'operation': 'session.delete',
            'method': 'DELETE',
            'status_code': 503,
        }
    else:
        (record,) = _diag(caplog, 'do_mars.control_transport_error')
        assert _fields(record) == {
            'operation': 'session.delete',
            'method': 'DELETE',
            'error_class': 'ConnectError',
        }
    _assert_private(caplog, *_private_markers())


def _service(do: DoMarsClient, http: httpx.AsyncClient) -> DoMarsSandboxService:
    return DoMarsSandboxService(
        user_context=ADMIN,
        db_session=AsyncMock(),
        httpx_client=http,
        max_num_sandboxes=10,
        sandbox_spec_service=AsyncMock(),
        do=do,
        do_bearer_token=TEAM_TOKEN,
        gateway_base_url=GATEWAY,
        callback_url='https://app.example.test',
    )


def _row() -> StoredSandbox:
    return StoredSandbox(
        id=SANDBOX_ID,
        backend=DO_MARS_BACKEND,
        created_by_user_id='owner',
        sandbox_spec_id='fixture',
        provider_resource_id=PROVIDER_ID,
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_delete_unconfirmed_logs_last_provider_status(caplog, monkeypatch):
    """Delete accepted, but the session never reaches a terminal status."""
    caplog.set_level(logging.INFO)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == 'DELETE':
            return httpx.Response(202)
        return httpx.Response(
            200,
            json={
                'session': {
                    'session_id': PROVIDER_ID,
                    'name': f'oh-{SANDBOX_ID}',
                    'status': 'SESSION_STATUS_DELETING',
                    'config_id': BODY_MARKER,
                }
            },
        )

    loop = asyncio.get_running_loop()
    clock = [loop.time()]

    def fast_time():
        clock[0] += 7
        return clock[0]

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = _service(DoMarsClient(TEAM_TOKEN, http), http)
        monkeypatch.setattr(loop, 'time', fast_time)
        with patch(
            'openhands.app_server.sandbox.do_mars_sandbox_service.asyncio.sleep',
            AsyncMock(),
        ):
            with pytest.raises(SandboxDeleteRetryError):
                await service._delete_at_provider(_row())
        monkeypatch.undo()
    (record,) = _diag(caplog, 'do_mars.delete_unconfirmed')
    assert _fields(record) == {
        'reason': 'confirmation_timeout',
        'provider_status': 'SESSION_STATUS_DELETING',
    }
    _assert_private(caplog, *_private_markers())


@pytest.mark.asyncio
async def test_delete_control_error_logs_status_and_class(caplog):
    caplog.set_level(logging.INFO)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(500, text=BODY_MARKER)
        )
    ) as http:
        service = _service(DoMarsClient(TEAM_TOKEN, http), http)
        with pytest.raises(SandboxDeleteRetryError):
            await service._delete_at_provider(_row())
    assert _fields(_diag(caplog, 'do_mars.control_response')[0])['status_code'] == 500
    (record,) = _diag(caplog, 'do_mars.delete_unconfirmed')
    assert _fields(record) == {'reason': 'control_error', 'error_class': 'SandboxError'}
    _assert_private(caplog, *_private_markers())


@contextlib.asynccontextmanager
async def _provider(handler):
    provider = web.Application()
    provider.router.add_get('/{tail:.*}', handler)
    runner = web.AppRunner(provider)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        yield f'ws://127.0.0.1:{port}/v2/agents/sessions/{PROVIDER_ID}/port-forward/8000?t={TEAM_TOKEN}'
    finally:
        await runner.cleanup()


async def _drive_one_connection(origin: str) -> None:
    host, port = origin.removeprefix('http://').split(':')
    reader, writer = await asyncio.open_connection(host, int(port))
    writer.write(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n')
    await writer.drain()
    await asyncio.wait_for(reader.read(), timeout=5)
    writer.close()


@pytest.mark.asyncio
async def test_tunnel_handshake_rejection_logs_status_only(caplog):
    caplog.set_level(logging.INFO)

    async def reject(request):
        return web.Response(status=403, text=f'{BODY_MARKER} {TEAM_TOKEN}')

    async with _provider(reject) as url:
        async with tcp_forward(url, TEAM_TOKEN) as origin:
            await _drive_one_connection(origin)
            await asyncio.sleep(0.1)
    (record,) = _diag(caplog, 'do_mars.tunnel_ws_failed')
    assert _fields(record) == {
        'phase': 'handshake',
        'handshake_status': 403,
        'error_class': 'WSServerHandshakeError',
    }
    _assert_private(caplog, *_private_markers('127.0.0.1'))


@pytest.mark.asyncio
async def test_tunnel_provider_text_frame_logs_type_not_text(caplog):
    caplog.set_level(logging.INFO)

    async def text_then_close(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_str(f'error: {BODY_MARKER} session {PROVIDER_ID}')
        await ws.close()
        return ws

    async with _provider(text_then_close) as url:
        async with tcp_forward(url, TEAM_TOKEN) as origin:
            await _drive_one_connection(origin)
            await asyncio.sleep(0.1)
    (record,) = _diag(caplog, 'do_mars.tunnel_ws_closed')
    assert _fields(record) == {'phase': 'provider_frame', 'frame_type': 'TEXT'}
    _assert_private(caplog, *_private_markers('127.0.0.1'))


@pytest.mark.asyncio
async def test_tunnel_request_post_failure_logs_method_and_attempts(caplog):
    caplog.set_level(logging.INFO)

    async def reject(request):
        return web.Response(status=502, text=BODY_MARKER)

    async with _provider(reject) as url:
        with pytest.raises(SandboxError):
            await tunnel_http_request(
                url,
                do_bearer_token=TEAM_TOKEN,
                http_host='localhost',
                method='POST',
                path='/api/init',
                headers={'X-Init-API-Key': 'SECRETINITKEY'},
                body=b'{"session_api_keys": ["SECRETSESSIONKEY"]}',
                timeout_seconds=5,
            )
    (record,) = _diag(caplog, 'do_mars.tunnel_request_failed')
    fields = _fields(record)
    assert fields['method'] == 'POST' and fields['attempts'] == 1
    assert fields['reason'] == 'transport_error' and fields['error_class']
    _assert_private(
        caplog, *_private_markers('127.0.0.1', 'SECRETINITKEY', 'SECRETSESSIONKEY')
    )


@pytest.mark.asyncio
async def test_idle_probe_failure_logs_status_only(caplog):
    caplog.set_level(logging.INFO)
    key = issue_session_key(SANDBOX_ID, PROVIDER_ID)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(503, text=BODY_MARKER)
        )
    ) as http:
        service = _service(AsyncMock(), http)
        sandbox = SandboxInfo(
            id=SANDBOX_ID,
            created_by_user_id='owner',
            sandbox_spec_id='fixture',
            status=SandboxStatus.RUNNING,
            session_api_key=key,
            exposed_urls=service.exposed_urls(_row()),
        )
        assert await service.get_idle_time(sandbox) is None
    (record,) = _diag(caplog, 'do_mars.idle_probe_failed')
    assert _fields(record) == {
        'operation': 'idle.probe',
        'reason': 'bad_status',
        'status_code': 503,
    }
    _assert_private(caplog, *_private_markers(key))


async def _store(async_session_maker) -> str:
    key = issue_session_key(SANDBOX_ID, PROVIDER_ID)
    now = datetime.now(UTC)
    async with async_session_maker() as db:
        db.add(
            StoredSandbox(
                id=SANDBOX_ID,
                backend=DO_MARS_BACKEND,
                created_by_user_id='owner',
                sandbox_spec_id='fixture',
                session_api_key=SecretStr(key),
                session_api_key_hash=hash_session_api_key(key),
                provider_resource_id=PROVIDER_ID,
                provider_create_intent_at=now,
                created_at=now,
                lifecycle_state='running',
            )
        )
        await db.commit()
    return key


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['guest_status', 'guest_transport'])
async def test_gateway_guest_failure_logs_status_or_class(
    async_session_maker, caplog, failure
):
    caplog.set_level(logging.INFO)
    key = await _store(async_session_maker)

    async def fail(request):
        return web.Response(status=500, text=f'{BODY_MARKER} {key}')

    guest = web.Application()
    guest.router.add_post('/{tail:.*}', fail)
    runner = web.AppRunner(guest)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    if failure == 'guest_transport':
        await runner.cleanup()

    @contextlib.asynccontextmanager
    async def forward(url, token):
        yield f'http://127.0.0.1:{port}'

    try:
        with (
            patch('storage.database.a_session_maker', async_session_maker),
            patch('openhands.app_server.sandbox.do_mars_proxy.tcp_forward', forward),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=gateway_app), base_url=GATEWAY
            ) as client:
                response = await client.post(
                    f'/{SANDBOX_ID}/8000/api/conversations/SECRETPATH',
                    headers={'X-Session-API-Key': key, 'Cookie': 'SECRETCOOKIE'},
                    content=b'SECRETREQUESTBODY',
                )
    finally:
        if failure == 'guest_status':
            await runner.cleanup()
    if failure == 'guest_status':
        assert response.status_code == 500
        (record,) = _diag(caplog, 'do_mars.gateway_guest_response')
        assert _fields(record) == {
            'operation': 'gateway.http',
            'method': 'POST',
            'status_code': 500,
        }
    else:
        assert response.status_code == 502
        (record,) = _diag(caplog, 'do_mars.gateway_guest_transport_error')
        fields = _fields(record)
        assert fields['method'] == 'POST' and fields['error_class']
    _assert_private(
        caplog,
        *_private_markers(
            key, 'SECRETPATH', 'SECRETCOOKIE', 'SECRETREQUESTBODY', '127.0.0.1'
        ),
    )


class _RejectingGuest(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = f'{BODY_MARKER} rejected'.encode()
        self.send_response(403)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.mark.asyncio
async def test_gateway_guest_ws_handshake_failure_logs_status_only(
    async_session_maker, caplog
):
    caplog.set_level(logging.INFO)
    key = await _store(async_session_maker)
    guest = ThreadingHTTPServer(('127.0.0.1', 0), _RejectingGuest)
    threading.Thread(target=guest.serve_forever, daemon=True).start()

    @contextlib.asynccontextmanager
    async def forward(url, token):
        yield f'http://127.0.0.1:{guest.server_address[1]}'

    try:
        with (
            patch('storage.database.a_session_maker', async_session_maker),
            patch('openhands.app_server.sandbox.do_mars_proxy.tcp_forward', forward),
            TestClient(gateway_app) as client,
        ):
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(
                    f'/{SANDBOX_ID}/8000/sockets/events/SECRETPATH',
                    headers={'X-Session-API-Key': key},
                ) as ws:
                    ws.receive_text()
    finally:
        guest.shutdown()
    (record,) = _diag(caplog, 'do_mars.gateway_guest_ws_failed')
    assert _fields(record) == {
        'operation': 'gateway.ws',
        'phase': 'handshake',
        'handshake_status': 403,
        'error_class': 'WSServerHandshakeError',
    }
    _assert_private(caplog, *_private_markers(key, 'SECRETPATH', '127.0.0.1'))
