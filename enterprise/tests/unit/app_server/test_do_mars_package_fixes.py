"""Regression tests for the portable DO MARS package corrections.

All database tests use the shared real-PostgreSQL fixtures. Only the DO
control plane is faked, because it is an external service.
"""

import asyncio
import socket
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import select, text

from openhands.agent_server.utils import utc_now
from openhands.app_server.errors import SandboxDeleteRetryError, SandboxError
from openhands.app_server.sandbox.do_mars_client import DO_MARS_BACKEND, DoMarsSession
from openhands.app_server.sandbox.do_mars_credentials import issue_session_key
from openhands.app_server.sandbox.do_mars_pool_config import DoMarsPoolConfig
from openhands.app_server.sandbox.do_mars_proxy import _safe_path
from openhands.app_server.sandbox.do_mars_sandbox_service import DoMarsSandboxService
from openhands.app_server.sandbox.do_mars_spec_service import DoMarsSpecInfo
from openhands.app_server.sandbox.do_mars_tunnel import tcp_forward
from openhands.app_server.sandbox.sandbox_models import (
    SandboxStartedBy,
    SandboxStatus,
)
from openhands.app_server.sandbox.sandbox_store import (
    StoredSandbox,
    hash_session_api_key,
    mark_running,
)
from openhands.app_server.user.specifiy_user_context import ADMIN
from tests import postgres_testdb

# Deterministic, obviously synthetic HS256 test key (not a credential).
SIGNING_KEY = 'synthetic-unit-test-' + 'k' * 32
OWNER = 'owner-a'
OTHER = 'owner-b'


def _uuid(n: int) -> str:
    return f'00000000-0000-4000-8000-{n:012d}'


def _user(user_id):
    user = AsyncMock()
    user.get_user_id.return_value = user_id
    return user


def _spec() -> DoMarsSpecInfo:
    return DoMarsSpecInfo(
        id='fixture',
        config_id='fixture-config',
        init_api_key=SecretStr('bootstrap'),
        command=None,
        working_dir='/workspace',
    )


def _service(db, user, provider, startup_timeout_seconds=300, pool_config=None):
    specs = AsyncMock()
    specs.get_sandbox_spec.return_value = _spec()
    return DoMarsSandboxService(
        user_context=user,
        db_session=db,
        httpx_client=AsyncMock(),
        max_num_sandboxes=10,
        sandbox_spec_service=specs,
        do=provider,
        do_bearer_token='fixture',
        gateway_base_url='https://runtime.example.invalid',
        callback_url='https://app.example.invalid',
        startup_timeout_seconds=startup_timeout_seconds,
        pool_config=pool_config,
    )


def _row(sid, owner, resource_id, key=None, intent_at=None) -> StoredSandbox:
    key = key or issue_session_key(sid, resource_id or _uuid(0))
    row = StoredSandbox(
        id=sid,
        backend=DO_MARS_BACKEND,
        created_by_user_id=owner,
        sandbox_spec_id='fixture',
        session_api_key=SecretStr(key),
        session_api_key_hash=hash_session_api_key(key),
        provider_resource_id=resource_id,
        provider_create_intent_at=intent_at or utc_now(),
        created_at=utc_now(),
    )
    mark_running(row)
    return row


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch):
    monkeypatch.setenv('DO_MARS_SESSION_SIGNING_KEY', SIGNING_KEY)


class _ConcurrentProvider:
    """Fake DO status reads that record how many overlap."""

    def __init__(self, failing: set[str] = frozenset()):
        self.active = 0
        self.max_active = 0
        self.failing = failing

    async def get_session(self, session_id):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.02)
            if session_id in self.failing:
                raise SandboxError('provider unavailable', status_code=502)
            return DoMarsSession(
                session_id, '', 'SESSION_STATUS_READY', 'fixture-config', None
            )
        finally:
            self.active -= 1


@pytest.mark.asyncio
async def test_batch_reads_ten_rows_serially_and_keeps_request_order(
    async_session_maker,
):
    async with async_session_maker() as db:
        for n in range(10):
            db.add(_row(f'sbx-{n}', OWNER, _uuid(n + 1)))
        db.add(_row('sbx-other', OTHER, _uuid(99)))
        await db.commit()

    provider = _ConcurrentProvider(failing={_uuid(7)})  # row sbx-6
    requested = [
        'sbx-3',
        'missing',
        'sbx-0',
        'sbx-3',
        'sbx-other',
        *[f'sbx-{n}' for n in range(10)],
    ]
    async with async_session_maker() as db:
        service = _service(db, _user(OWNER), provider)
        results = await service.batch_get_sandboxes(requested)

    assert len(results) == len(requested)
    for sid, info in zip(requested, results, strict=True):
        if sid in ('missing', 'sbx-other'):
            assert info is None, sid
        else:
            assert info is not None and info.id == sid
    by_id = {info.id: info for info in results if info is not None}
    assert by_id['sbx-6'].status == SandboxStatus.STARTING
    assert by_id['sbx-6'].session_api_key is None
    assert by_id['sbx-0'].status == SandboxStatus.RUNNING
    assert results[0] is not results[3]
    assert results[0].id == results[3].id == 'sbx-3'
    # Provider reads still overlap; only the DB reads were serialized.
    assert provider.max_active > 1


@pytest.mark.asyncio
async def test_expired_session_key_cannot_authenticate_secret_reads(
    async_session_maker,
):
    with patch(
        'openhands.app_server.sandbox.do_mars_credentials.time.time',
        return_value=1_000,
    ):
        expired = issue_session_key('sbx-expired', _uuid(1))
    fresh = issue_session_key('sbx-fresh', _uuid(2))
    async with async_session_maker() as db:
        db.add(_row('sbx-expired', OWNER, _uuid(1), key=expired))
        db.add(_row('sbx-fresh', OWNER, _uuid(2), key=fresh))
        await db.commit()

    async with async_session_maker() as db:
        service = _service(db, ADMIN, _ConcurrentProvider())
        assert await service.get_sandbox_by_session_api_key(expired) is None
        assert await service.get_sandbox_record_by_session_api_key(expired) is None
        info = await service.get_sandbox_by_session_api_key(fresh)
        assert info is not None and info.id == 'sbx-fresh'
        record = await service.get_sandbox_record_by_session_api_key(fresh)
        assert record is not None and record.id == 'sbx-fresh'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'listed',
    [
        [],
        [
            DoMarsSession(_uuid(7), 'oh-sbx-u', 'S', 'fixture-config', None),
            DoMarsSession(_uuid(8), 'oh-sbx-u', 'S', 'fixture-config', None),
        ],
    ],
    ids=['zero-matches-long-after-intent', 'ambiguous-duplicates'],
)
async def test_unresolved_create_intent_is_retained_for_reconciliation(
    async_session_maker, listed
):
    """No elapsed-time rule may clear a pending intent: an empty list is not a
    provider guarantee that a delayed create cannot still land."""
    intent_at = utc_now() - timedelta(days=2)
    async with async_session_maker() as db:
        db.add(_row('sbx-u', OWNER, None, intent_at=intent_at))
        await db.commit()

    provider = AsyncMock()
    provider.list_sessions_by_name.return_value = listed
    async with async_session_maker() as db:
        service = _service(db, _user(OWNER), provider)
        with pytest.raises(SandboxDeleteRetryError):
            await service.delete_sandbox('sbx-u')
        await db.rollback()
    provider.create_session.assert_not_called()
    provider.delete_session.assert_not_called()
    async with async_session_maker() as db:
        remaining = await db.scalar(
            select(StoredSandbox).where(StoredSandbox.id == 'sbx-u')
        )
    assert remaining is not None
    assert remaining.provider_resource_id is None
    assert remaining.provider_create_intent_at == intent_at


@pytest.mark.parametrize(
    'path',
    ['api//init', 'API/init', '/api/init', 'x%3Fy', 'a?b', 'a#b', 'a\x00b', 'a\nb'],
)
def test_gateway_path_boundary_rejects_new_forms(path):
    with pytest.raises(HTTPException):
        _safe_path(path)


@pytest.mark.parametrize('path', ['', 'api/conversations', 'sockets/events/x'])
def test_gateway_path_boundary_allows_normal_paths(path):
    _safe_path(path)


@pytest.mark.asyncio
async def test_loopback_forward_accepts_exactly_one_local_connection():
    hold = asyncio.Event()

    class FakeWs:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            await hold.wait()
            raise StopAsyncIteration

        async def send_bytes(self, data):
            pass

    with patch('aiohttp.ClientSession.ws_connect', lambda *a, **k: FakeWs()):
        async with tcp_forward('wss://provider.example.invalid/x', 't') as origin:
            port = int(origin.rsplit(':', 1)[1])
            first = socket.create_connection(('127.0.0.1', port), timeout=2)
            await asyncio.sleep(0.1)
            try:
                second = socket.create_connection(('127.0.0.1', port), timeout=2)
            except OSError:
                pass  # the listener already stopped accepting
            else:
                second.settimeout(2)
                assert await asyncio.to_thread(second.recv, 1) == b''
                second.close()
            hold.set()
            first.close()


def test_injector_never_serializes_team_token(monkeypatch):
    from openhands.app_server.sandbox.do_mars_sandbox_service import (
        DoMarsSandboxServiceInjector,
    )

    monkeypatch.setenv('DO_MARS_TEAM_TOKEN', 'team-token-must-not-serialize')
    monkeypatch.setenv('DO_MARS_GATEWAY_URL', 'https://runtime.example.invalid')
    injector = DoMarsSandboxServiceInjector()
    assert injector.max_num_sandboxes == 10
    assert 'team-token-must-not-serialize' not in repr(injector)
    assert 'team-token-must-not-serialize' not in injector.model_dump_json()
    assert injector.do_team_token.get_secret_value() == (
        'team-token-must-not-serialize'
    )


def test_migration_is_rebased_on_release_head_and_survives_stamp_rollback(
    test_database, engine
):
    server, name = test_database.server, test_database.name

    def columns():
        with engine.connect() as conn:
            return set(
                conn.execute(
                    text(
                        'SELECT column_name FROM information_schema.columns '
                        "WHERE table_name = 'v1_remote_sandbox' AND column_name "
                        "IN ('provider_resource_id', 'provider_create_intent_at')"
                    )
                ).scalars()
            )

    def version():
        with engine.connect() as conn:
            return conn.execute(
                text('SELECT version_num FROM alembic_version')
            ).scalar()

    expected = {'provider_resource_id', 'provider_create_intent_at'}
    # 'head' is now do_mars_002 (the warm-pool migration, do_mars_001's
    # child); its own table is unrelated to these two v1_remote_sandbox
    # columns, which is why they are still exactly `expected` here.
    assert version() == 'do_mars_002' and columns() == expected
    # Data-preserving rollback: stock image sees 213, columns stay.
    postgres_testdb.run_alembic(server, name, 'stamp', '213')
    assert version() == '213' and columns() == expected
    # Reinstall is idempotent on the kept columns.
    postgres_testdb.run_alembic(server, name, 'upgrade', 'head')
    assert version() == 'do_mars_002' and columns() == expected
    # Full downgrade removes them; upgrade restores them.
    postgres_testdb.run_alembic(server, name, 'downgrade', '213')
    assert version() == '213' and columns() == set()
    postgres_testdb.run_alembic(server, name, 'upgrade', 'head')
    assert columns() == expected


@pytest.mark.asyncio
async def test_pool_claim_is_scoped_to_the_resolved_spec_config_id(async_session_maker):
    """A pool claim must be keyed on the provider's real config id for this
    spec (spec.config_id - the same value start_sandbox's own create path
    passes to do.create_session), never a separate pool-only label.

    A prior revision passed an independently-settable ``pool_config_id``
    (defaulting to the literal string "default") to claim_one instead. That
    value could silently diverge from the DO config a spec actually
    resolves to, so if an operator rotated the spec's provider config, a
    pool slot created under the *old* config could still be claimed for a
    request now resolving to the *new* one. Keying the claim on
    spec.config_id directly means a rotated-out config's slots simply stop
    matching - with no extra validation required, and no path for this
    service to ask the provider for a VM under one config while believing
    it is handing out the other.
    """
    spec = _spec()
    assert spec.config_id == 'fixture-config'

    with patch(
        'openhands.app_server.sandbox.do_mars_sandbox_service.claim_one',
        new=AsyncMock(return_value=None),
    ) as claim_one_mock:
        async with async_session_maker() as db:
            service = _service(
                db,
                _user(OWNER),
                AsyncMock(),
                pool_config=DoMarsPoolConfig(
                    enabled=True,
                    sandbox_spec_id=spec.id,
                    min_hot=1,
                    min_paused_reserve=0,
                    max_total=1,
                ),
            )
            result = await service._start_sandbox_from_pool(
                spec,
                'sbx-pool-claim',
                SandboxStartedBy.USER,
                None,
                OWNER,
                None,
            )

    assert result is None  # claim_one mocked to find nothing: no fall-through side effects.
    claim_one_mock.assert_awaited_once()
    assert claim_one_mock.await_args.kwargs['config_id'] == spec.config_id
    assert claim_one_mock.await_args.kwargs['config_id'] != 'default'


# NOTE: no executable regression test here for the UUID-org claimed_org_id
# fix in _start_sandbox_from_pool (do_mars_sandbox_service.py). That
# module - and this whole test file - require
# `from openhands.agent_server.utils import utc_now` at module scope,
# which this sandbox genuinely cannot import (no network access to fetch
# openhands-agent-server, and it is not part of the stub tree by design -
# see enterprise/tests/conftest.py's own docstring). This is the exact
# same documented limitation as session 2's pool_config/PrivateAttr fix in
# this same file: verified here only by py_compile and manual review of
# the call site (claimed_org_id=str(org) if org is not None else None,
# credential_org_id=org left untouched a few lines below for the owned
# StoredSandbox row's own ORM-typed column), not by a test in this
# sandbox. An environment with the real Enterprise/agent-server tree
# should add a test asserting claim_one receives a str even when `org` is
# a uuid.UUID, and that credential_org_id on the resulting StoredSandbox
# row is untouched.
