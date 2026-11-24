from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.responses import PlainTextResponse
from starlette.applications import Starlette
from starlette.routing import Host

from openhands.app_server.sandbox import do_mars_proxy as gateway
from openhands.app_server.sandbox.do_mars_credentials import issue_session_key
from openhands.app_server.sandbox.do_mars_preview import (
    PREVIEW_COOKIE,
    PREVIEW_SESSION_PATH,
    preview_domain,
)
from openhands.app_server.sandbox.sandbox_store import hash_session_api_key

APP = 'https://app.example.test'
DOMAIN = 'preview.app.example.test'
TARGET = f'https://sandbox-a.{DOMAIN}'
PROVIDER = '00000000-0000-4000-8000-000000000001'


@pytest.fixture
def preview(monkeypatch):
    monkeypatch.setenv(
        'DO_MARS_SESSION_SIGNING_KEY', 'synthetic-preview-key-' + 'q' * 32
    )
    monkeypatch.setattr(gateway, 'APP_ORIGIN', APP)
    key = issue_session_key('sandbox-a', PROVIDER)
    row = gateway.GatewayRow(PROVIDER, hash_session_api_key(key), 'running')
    monkeypatch.setattr(gateway, '_load_row', AsyncMock(return_value=row))
    forward = AsyncMock(return_value=PlainTextResponse('synthetic preview'))
    monkeypatch.setattr(gateway, '_proxy_guest_http', forward)
    app = Starlette(
        routes=[Host('{sandbox_id}.' + DOMAIN, gateway.build_preview_app(DOMAIN))]
    )
    return httpx.ASGITransport(app=app), key, row, forward


@pytest.mark.asyncio
async def test_preview_exchange_and_forward_are_bound_to_sandbox(preview):
    transport, key, row, forward = preview
    async with httpx.AsyncClient(transport=transport, base_url=TARGET) as client:
        assert (await client.get('/')).status_code == 401
        assert (
            await client.post(PREVIEW_SESSION_PATH, headers={'origin': APP})
        ).status_code == 401
        rejected = await client.post(
            PREVIEW_SESSION_PATH, headers={'origin': TARGET, 'X-Session-API-Key': key}
        )
        assert rejected.status_code == 403
        reply = await client.post(
            PREVIEW_SESSION_PATH, headers={'origin': APP, 'X-Session-API-Key': key}
        )
        assert reply.status_code == 204
        assert reply.headers['access-control-allow-origin'] == APP
        assert reply.headers['access-control-allow-credentials'] == 'true'
        cookie = reply.headers['set-cookie']
        assert all(
            value in cookie.lower()
            for value in ('secure', 'httponly', 'samesite=strict', 'path=/')
        )
        assert 'domain=' not in cookie.lower()
        assert reply.headers['cache-control'] == 'no-store'
        assert (await client.get(PREVIEW_SESSION_PATH)).status_code == 404
        assert (await client.post('/check', headers={'origin': APP})).status_code == 403
        assert (await client.get('/')).text == 'synthetic preview'
        assert forward.await_args.args[1:] == (row, 8011, '')

        token = cookie.split(';', 1)[0]
        copied = await client.get(
            f'https://sandbox-b.{DOMAIN}/', headers={'cookie': token}
        )
        assert copied.status_code == 401
        assert forward.await_count == 1


@pytest.mark.asyncio
async def test_paused_or_rekeyed_preview_never_dials(preview):
    transport, key, row, forward = preview
    async with httpx.AsyncClient(transport=transport, base_url=TARGET) as client:
        headers = {'origin': APP, 'X-Session-API-Key': key}
        reply = await client.post(PREVIEW_SESSION_PATH, headers=headers)
        assert reply.status_code == 204
        assert client.cookies.get(PREVIEW_COOKIE)
        row.lifecycle_state = 'paused'
        assert (await client.get('/')).status_code == 409
        assert (
            await client.post(PREVIEW_SESSION_PATH, headers=headers)
        ).status_code == 409
        row.lifecycle_state = 'running'
        row.session_api_key_hash = hash_session_api_key('synthetic-rotation')
        assert (await client.get('/')).status_code == 401
        forward.assert_not_called()


def test_preview_requires_explicit_opt_in_and_https(monkeypatch):
    monkeypatch.delenv('DO_MARS_ENABLE_BROWSER_PREVIEW', raising=False)
    assert preview_domain(APP) is None
    monkeypatch.setenv('DO_MARS_ENABLE_BROWSER_PREVIEW', 'true')
    assert preview_domain(APP) == DOMAIN
    with pytest.raises(ValueError):
        preview_domain('http://app.example.test')
