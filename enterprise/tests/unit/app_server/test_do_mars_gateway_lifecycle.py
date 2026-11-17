"""Gateway refuses settled non-RUNNING rows (real PostgreSQL, no provider).

MARS port-forwarding can wake a paused VM, so every denial here must happen
before ``tcp_forward`` is called.
"""

import asyncio
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from openhands.agent_server.utils import utc_now
from openhands.app_server.sandbox.do_mars_client import DO_MARS_BACKEND
from openhands.app_server.sandbox.do_mars_credentials import issue_session_key
from openhands.app_server.sandbox.do_mars_proxy import app, authorize
from openhands.app_server.sandbox.sandbox_store import (
    StoredSandbox,
    hash_session_api_key,
)

# Deterministic, obviously synthetic HS256 test key (not a credential).
SIGNING_KEY = 'synthetic-unit-test-' + 'k' * 32
SID = 'sbx-gateway'
PROVIDER_ID = '00000000-0000-4000-8000-000000000042'
EVENTS = '/sbx-gateway/8000/sockets/events/00000000-0000-4000-8000-000000000002'


class NoDial:
    """Stands in for ``tcp_forward`` and fails the test if it is reached."""

    calls = 0

    def __call__(self, *args, **kwargs):
        NoDial.calls += 1
        raise AssertionError('provider dial attempted')


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv('DO_MARS_SESSION_SIGNING_KEY', SIGNING_KEY)
    monkeypatch.setenv('DO_MARS_TEAM_TOKEN', 'synthetic-team-token')
    NoDial.calls = 0


@pytest.fixture
def gateway_db(async_session_maker):
    with (
        patch('storage.database.a_session_maker', async_session_maker),
        patch('openhands.app_server.sandbox.do_mars_proxy.tcp_forward', NoDial()),
    ):
        yield async_session_maker


async def _store(async_session_maker, state: str) -> str:
    key = issue_session_key(SID, PROVIDER_ID)
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
                provider_create_intent_at=utc_now(),
                created_at=utc_now(),
                lifecycle_state=state,
            )
        )
        await db.commit()
    return key


@pytest.mark.asyncio
async def test_running_row_is_authorized(gateway_db):
    key = await _store(gateway_db, 'running')
    row = await authorize(SID, 8000, key)
    assert row.provider_resource_id == PROVIDER_ID


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['paused', 'unexpected-state'])
async def test_non_running_row_is_refused_after_key_check(gateway_db, state):
    key = await _store(gateway_db, state)
    with pytest.raises(HTTPException) as wrong_key:
        await authorize(SID, 8000, 'wrong-key')
    # State is never revealed to an unauthenticated caller.
    assert wrong_key.value.status_code == 401
    with pytest.raises(HTTPException) as refused:
        await authorize(SID, 8000, key)
    assert refused.value.status_code == 409


@pytest.mark.asyncio
async def test_row_lock_held_by_worker_does_not_block_or_refuse(gateway_db):
    """The lifecycle worker probes through the gateway while holding this lock."""
    key = await _store(gateway_db, 'running')
    async with gateway_db() as worker:
        await worker.execute(
            select(StoredSandbox).where(StoredSandbox.id == SID).with_for_update()
        )
        row = await asyncio.wait_for(authorize(SID, 8000, key), timeout=5)
        assert row.provider_resource_id == PROVIDER_ID
        with pytest.raises(HTTPException) as wrong_key:
            await authorize(SID, 8000, 'wrong-key')
        assert wrong_key.value.status_code == 401
        await worker.rollback()


@pytest.mark.asyncio
async def test_paused_row_http_never_dials(gateway_db):
    key = await _store(gateway_db, 'paused')
    with TestClient(app) as client:
        response = client.get(
            '/sbx-gateway/8000/api/conversations', headers={'X-Session-API-Key': key}
        )
        post = client.post(
            '/sbx-gateway/8000/api/conversations',
            headers={'X-Session-API-Key': key},
            json={},
        )
    assert response.status_code == post.status_code == 409
    assert NoDial.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('auth', ['header', 'query', 'first-frame'])
async def test_paused_row_websocket_never_dials(gateway_db, auth):
    key = await _store(gateway_db, 'paused')
    url, headers = EVENTS, {}
    if auth == 'header':
        headers = {'X-Session-API-Key': key}
    elif auth == 'query':
        url = f'{EVENTS}?session_api_key={key}'
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(url, headers=headers) as ws:
                if auth == 'first-frame':
                    ws.send_json({'type': 'auth', 'session_api_key': key})
                ws.receive_text()
    assert NoDial.calls == 0
