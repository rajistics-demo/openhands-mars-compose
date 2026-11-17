"""Provider wire format, authorization boundary, and lifecycle crash safety."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from aiohttp import web
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import select

from openhands.app_server.errors import SandboxError
from openhands.app_server.sandbox.do_mars_client import DoMarsClient, DoMarsSession
from openhands.app_server.sandbox.do_mars_proxy import GatewayRow, _safe_path, authorize
from openhands.app_server.sandbox.do_mars_sandbox_service import (
    DoMarsSandboxService,
    _status_for,
)
from openhands.app_server.sandbox.do_mars_spec_service import DoMarsSpecInfo
from openhands.app_server.sandbox.do_mars_tunnel import tunnel_http_request
from openhands.app_server.sandbox.sandbox_models import SandboxStatus
from openhands.app_server.sandbox.sandbox_store import (
    StoredSandbox,
    hash_session_api_key,
)

SID = '00000000-0000-4000-8000-000000000001'
# Deterministic, obviously synthetic HS256 test key (not a credential).
SYNTHETIC_SIGNING_KEY = 'synthetic-unit-test-' + 'k' * 32


@pytest.mark.asyncio
async def test_control_wire_envelope_and_no_redirect_or_raw_errors():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            201,
            json={
                'session': {
                    'session_id': SID,
                    'name': 'oh-fixture',
                    'status': 'SESSION_STATUS_READY',
                    'config_id': 'fixture',
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DoMarsClient('test-provider-token', http)
        session = await client.create_session('oh-fixture', 'fixture')
        assert session.id == SID
        assert seen[0].url.host == 'api.digitalocean.com'
        with pytest.raises(ValueError):
            await client.delete_session('../configs/fixture')
        assert len(seen) == 1


@pytest.mark.asyncio
async def test_unauthorized_and_preview_requests_never_dial_provider(monkeypatch):
    monkeypatch.setenv('DO_MARS_SESSION_SIGNING_KEY', SYNTHETIC_SIGNING_KEY)
    from openhands.app_server.sandbox.do_mars_credentials import issue_session_key

    key = issue_session_key('fixture', SID)
    row = GatewayRow(SID, hash_session_api_key(key), 'running')
    with patch(
        'openhands.app_server.sandbox.do_mars_proxy._load_row',
        AsyncMock(return_value=row),
    ) as load:
        with pytest.raises(HTTPException) as exc:
            await authorize('fixture', 8001, key)
        assert exc.value.status_code == 404
        load.assert_not_called()
        for invalid_key in [None, 'wrong-key']:
            with pytest.raises(HTTPException) as exc:
                await authorize('fixture', 8000, invalid_key)
            assert exc.value.status_code == 401
        assert await authorize('fixture', 8000, key) == row


@pytest.mark.parametrize(
    'path', ['api/init', 'api%2finit', 'api%252finit', '../api/init', 'api/init/']
)
def test_external_init_is_blocked(path):
    with pytest.raises(HTTPException):
        _safe_path(path)


def test_unknown_and_terminal_provider_statuses_fail_closed():
    assert (
        _status_for(DoMarsSession(SID, '', 'NEW_UNKNOWN_STATE', None, None))
        == SandboxStatus.ERROR
    )
    assert (
        _status_for(DoMarsSession(SID, '', 'SESSION_STATUS_DESTROYED', None, None))
        == SandboxStatus.MISSING
    )


@pytest.mark.asyncio
async def test_chunked_http_and_no_replay_of_ambiguous_post():
    requests = []

    async def port_forward(request):
        assert request.headers['Authorization'] == 'Bearer fixture-token'
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        message = await ws.receive()
        requests.append(message.data)
        if b'POST ' in message.data:
            await ws.close()
            return ws
        await ws.send_bytes(
            b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n'
        )
        await asyncio.sleep(0.05)
        await ws.send_bytes(b'6\r\n world\r\n0\r\n\r\n')
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get('/forward', port_forward)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        args = dict(
            do_bearer_token='fixture-token',
            http_host='localhost',
            path='/probe',
            headers={},
            timeout_seconds=3,
        )
        response = await tunnel_http_request(
            f'http://127.0.0.1:{port}/forward', method='GET', **args
        )
        assert response.body == b'hello world'
        with pytest.raises(SandboxError):
            await tunnel_http_request(
                f'http://127.0.0.1:{port}/forward', method='POST', body=b'{}', **args
            )
        assert len(requests) == 2
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_create_intent_and_mapping_are_committed_before_guest_init(
    async_session_maker,
    monkeypatch,
):
    monkeypatch.setenv('DO_MARS_SESSION_SIGNING_KEY', SYNTHETIC_SIGNING_KEY)
    user = AsyncMock()
    user.get_user_id.return_value = 'fixture-owner'
    user.get_effective_org_id.return_value = None
    user.get_default_sandbox_spec_id.return_value = None
    user.resolver_org_id = None
    spec = DoMarsSpecInfo(
        id='fixture',
        config_id='fixture-config',
        init_api_key=SecretStr('bootstrap'),
        command=None,
        working_dir='/workspace',
    )
    specs = AsyncMock()
    specs.get_default_sandbox_spec.return_value = spec
    provider = AsyncMock()

    async def create(name, config):
        async with async_session_maker() as observer:
            saved = (
                await observer.scalars(
                    select(StoredSandbox).where(StoredSandbox.id == 'fixture-durable')
                )
            ).one()
            assert saved.provider_create_intent_at is not None
            assert saved.provider_resource_id is None
        return DoMarsSession(
            SID, name, 'SESSION_STATUS_READY', config, datetime.now(UTC).isoformat()
        )

    provider.create_session.side_effect = create
    provider.get_session.return_value = DoMarsSession(
        SID, 'oh-fixture-durable', 'SESSION_STATUS_READY', 'fixture-config', None
    )
    async with async_session_maker() as db:
        service = DoMarsSandboxService(
            user_context=user,
            db_session=db,
            httpx_client=AsyncMock(),
            max_num_sandboxes=3,
            sandbox_spec_service=specs,
            do=provider,
            do_bearer_token='fixture',
            gateway_base_url='https://runtime.example.test',
            callback_url='https://app.example.test',
        )

        async def init(*args):
            async with async_session_maker() as observer:
                saved = (
                    await observer.scalars(
                        select(StoredSandbox).where(
                            StoredSandbox.id == 'fixture-durable'
                        )
                    )
                ).one()
                assert saved.provider_resource_id == SID
            raise SandboxError('Synthetic init failure')

        with (
            patch.object(service, 'pause_old_sandboxes', AsyncMock()),
            patch.object(service, '_init_agent_server', init),
        ):
            with pytest.raises(SandboxError):
                await service.start_sandbox(sandbox_id='fixture-durable')
        await db.rollback()
    async with async_session_maker() as observer:
        saved = (
            await observer.scalars(
                select(StoredSandbox).where(StoredSandbox.id == 'fixture-durable')
            )
        ).one()
        assert saved.provider_resource_id == SID


def test_session_credential_is_bound_to_one_vm_and_expires(monkeypatch):
    from openhands.app_server.sandbox.do_mars_credentials import (
        issue_session_key,
        valid_session_key,
    )

    monkeypatch.setenv('DO_MARS_SESSION_SIGNING_KEY', SYNTHETIC_SIGNING_KEY)
    token = issue_session_key('fixture', SID)
    assert valid_session_key(token, 'fixture', SID)
    assert not valid_session_key(token, 'other-sandbox', SID)
    assert not valid_session_key(
        token, 'fixture', '00000000-0000-4000-8000-000000000002'
    )
    with patch(
        'openhands.app_server.sandbox.do_mars_credentials.time.time', return_value=1
    ):
        expired = issue_session_key('fixture', SID)
    assert not valid_session_key(expired, 'fixture', SID)


@pytest.mark.asyncio
async def test_deferred_init_exports_canonical_key_without_provider_token():
    import json

    from openhands.app_server.sandbox.do_mars_tunnel import TunnelHttpResponse

    service = DoMarsSandboxService(
        user_context=AsyncMock(),
        db_session=AsyncMock(),
        httpx_client=AsyncMock(),
        max_num_sandboxes=6,
        sandbox_spec_service=AsyncMock(),
        do=AsyncMock(),
        do_bearer_token='provider-secret-must-not-reach-guest',
        gateway_base_url='https://runtime.example.test',
        callback_url='https://callback.example.test',
    )
    service.do.port_forward_url = lambda *args: 'wss://provider.example.test/forward'
    row = StoredSandbox(
        id='fixture',
        session_api_key=SecretStr('per-vm-session-key'),
        backend='do-mars',
        sandbox_spec_id='fixture',
    )
    spec = DoMarsSpecInfo(
        id='fixture',
        config_id='fixture',
        init_api_key=SecretStr('bootstrap-key'),
        command=None,
        working_dir='/workspace',
    )
    session = DoMarsSession(SID, 'fixture', 'SESSION_STATUS_READY', 'fixture', None)
    with patch(
        'openhands.app_server.sandbox.do_mars_sandbox_service.tunnel_http_request',
        AsyncMock(
            side_effect=[
                TunnelHttpResponse(200, {}, b'{"state":"dormant"}'),
                TunnelHttpResponse(200, {}, b'{}'),
            ]
        ),
    ) as tunnel:
        await service._init_agent_server(
            row, session, spec, {'OH_SESSION_API_KEYS_0': 'must-not-override'}
        )
        payload = json.loads(tunnel.call_args.kwargs['body'])
        assert payload['env']['OH_SESSION_API_KEYS_0'] == 'per-vm-session-key'
        assert payload['session_api_keys'] == ['per-vm-session-key']
        assert 'provider-secret-must-not-reach-guest' not in json.dumps(payload)


def test_ws_first_frame_auth_is_checked_before_provider_dial(monkeypatch):
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from openhands.app_server.sandbox.do_mars_proxy import app

    row = GatewayRow(SID, hash_session_api_key('correct-but-not-supplied'), 'running')
    with (
        patch(
            'openhands.app_server.sandbox.do_mars_proxy._load_row',
            AsyncMock(return_value=row),
        ),
        patch('openhands.app_server.sandbox.do_mars_proxy.tcp_forward') as dial,
    ):
        with TestClient(app).websocket_connect(
            '/fixture/8000/sockets/events/00000000-0000-4000-8000-000000000002'
        ) as ws:
            ws.send_json({'type': 'auth', 'session_api_key': 'wrong-key'})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()
        dial.assert_not_called()
