"""Real-PostgreSQL tests for the do_mars_doks_002 migration (DOKS package) and pool store.

Deploy this file at ``tests/unit/app_server/test_do_mars_pool_store.py`` in a
1.97.1 Enterprise checkout with the DOKS overlay already applied (see
``doks/docs/VERSIONS.md``) and run against a disposable PostgreSQL 17
instance from that checkout's own root:

    MARS_POOL_TEST_DSN=postgresql+asyncpg://openhands:openhands@localhost:15432/openhands \
        pytest tests/unit/app_server/test_do_mars_pool_store.py

That checkout's own ``tests/conftest.py`` and ``pytest.ini`` (``pythonpath
= .``, ``asyncio_mode = auto``) are what make
``from openhands.app_server...`` and the bare ``@pytest.mark.asyncio``
below resolve; running this file by itself outside that tree (e.g. by
absolute path from an unrelated cwd) skips both and fails confusingly
instead. Beyond that, these tests do not depend on anything licensed:
they exercise only the new do_mars_pool_* modules and the
do_mars_doks_002 migration against a real database, which is what the
store's transaction-safety claims (SELECT FOR UPDATE SKIP LOCKED,
commit-before-provider-call) actually depend on. See
do_mars_pool_reconciler tests for the fake-provider, no-database
business-logic tests.
"""

import importlib.util
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, MetaData, String, Table, create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from openhands.app_server.sandbox.do_mars_pool_models import PoolSlotState
from openhands.app_server.sandbox.do_mars_pool_store import (
    ClaimPromotionError,
    claim_one,
    count_by_state,
    delete_slot,
    get_slot,
    insert_creating_slot,
    lock_stale_claiming_slot_for_recovery,
    promote_claim_to_sandbox,
    select_sweepable,
    set_state,
)

pytestmark = pytest.mark.asyncio

DSN = os.environ.get(
    'MARS_POOL_TEST_DSN',
    'postgresql+asyncpg://openhands:openhands@localhost:15432/openhands',
)
SYNC_DSN = DSN.replace('+asyncpg', '+psycopg2')

_MIGRATION_FILENAME = 'do_mars_doks_002_add_pool_inventory.py'


def _find_migration_path() -> Path:
    """Locate the deployed migration file by walking up from this file.

    This file is copied to two different places with two different
    depths below the nearest project root - a 1.97.1 Enterprise checkout
    (``<checkout>/tests/unit/app_server/``, migration at
    ``<checkout>/migrations/versions/``) for a real import-backed run,
    and this source package's own
    ``doks/enterprise/tests-overlay/unit/app_server/`` for inspection
    (migration at ``doks/enterprise/overlay-migrations/``, no
    ``migrations/versions`` directory at all). A single hardcoded
    ``parents[N]`` silently resolved to the wrong, unrelated ancestor
    (even landing outside the project root, e.g. ``/tmp``) under
    whichever of the two this file was *not* written for; searching
    upward for either real layout fails loudly with a clear
    FileNotFoundError instead when neither is found.
    """
    for ancestor in Path(__file__).resolve().parents:
        candidate = ancestor / 'migrations' / 'versions' / _MIGRATION_FILENAME
        if candidate.is_file():
            return candidate
        candidate = ancestor / 'doks' / 'enterprise' / 'overlay-migrations' / _MIGRATION_FILENAME
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f'Could not locate {_MIGRATION_FILENAME} above {__file__} under either '
        "a deployed Enterprise checkout's migrations/versions/ or this "
        'package\'s own doks/enterprise/overlay-migrations/.'
    )


MIGRATION_PATH = _find_migration_path()


def _load_migration():
    spec = importlib.util.spec_from_file_location('do_mars_doks_002', MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module', autouse=True)
def _migrated_schema():
    """Apply do_mars_doks_002 once per test module, and prove it is reversible."""

    engine = create_engine(SYNC_DSN)
    migration = _load_migration()
    with engine.connect() as conn:
        op = Operations(MigrationContext.configure(conn))
        migration.op = op
        migration.downgrade()
        conn.commit()
        migration.upgrade()
        conn.commit()
    yield
    with engine.connect() as conn:
        conn.execute(text('TRUNCATE TABLE do_mars_pool_slot'))
        conn.commit()
    engine.dispose()


@pytest.fixture(autouse=True)
async def _clean_table():
    engine = create_async_engine(DSN)
    async with engine.begin() as conn:
        await conn.execute(text('TRUNCATE TABLE do_mars_pool_slot'))
        await conn.execute(text('TRUNCATE TABLE test_do_mars_fake_app_sandbox'))
    await engine.dispose()
    yield


@pytest.fixture
async def db_session():
    engine = create_async_engine(DSN)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session
    await engine.dispose()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# A stand-in for the real v1_remote_sandbox/StoredSandbox table. The real
# Enterprise ORM model is not available outside the licensed source tree,
# but promote_claim_to_sandbox only needs *a* table in the same database to
# prove the cross-table atomicity claim - what its columns are named is not
# part of what is under test here.
_fake_meta = MetaData()
fake_app_sandbox_table = Table(
    'test_do_mars_fake_app_sandbox',
    _fake_meta,
    Column('id', String, primary_key=True),
    Column('provider_resource_id', String, nullable=False),
)


@pytest.fixture(scope='module', autouse=True)
def _fake_app_sandbox_schema():
    engine = create_engine(SYNC_DSN)
    with engine.connect() as conn:
        # DROP + CREATE, not IF NOT EXISTS-and-keep-forever: this table must
        # start empty every test run, since prior runs' rows (e.g. the fixed
        # id 'race-app-sandbox') would otherwise make a later run's insert
        # collide on the primary key and fail for an unrelated reason.
        conn.execute(text('DROP TABLE IF EXISTS test_do_mars_fake_app_sandbox'))
        conn.execute(
            text(
                'CREATE TABLE test_do_mars_fake_app_sandbox ('
                'id VARCHAR PRIMARY KEY, provider_resource_id VARCHAR NOT NULL)'
            )
        )
        conn.commit()
    yield
    engine.dispose()


async def _make_hot_slot(
    db_session,
    slot_id,
    *,
    backend='do-mars',
    sandbox_spec_id='do-mars-agent-server',
    config_id='cfg-1',
    healthy=True,
    health_checked_at: datetime | None = None,
):
    await insert_creating_slot(
        db_session,
        slot_id=slot_id,
        backend=backend,
        sandbox_spec_id=sandbox_spec_id,
        config_id=config_id,
        intent_at=_utc_now(),
    )
    await set_state(db_session, slot_id, PoolSlotState.BOOTING)
    await set_state(
        db_session,
        slot_id,
        PoolSlotState.HOT_READY,
        provider_resource_id=f'provider-{slot_id}',
        last_health_ok=healthy,
    )
    if health_checked_at is not None:
        from sqlalchemy import text as _text

        await db_session.execute(
            _text(
                'UPDATE do_mars_pool_slot SET last_health_check_at = :ts '
                'WHERE id = :id'
            ),
            {'ts': health_checked_at, 'id': slot_id},
        )
        await db_session.commit()
    return slot_id


async def test_insert_creating_slot_is_durable_before_any_provider_call(db_session):
    await insert_creating_slot(
        db_session,
        slot_id='slot-a',
        backend='do-mars',
        sandbox_spec_id='spec',
        config_id='cfg',
        intent_at=_utc_now(),
    )
    slot = await get_slot(db_session, 'slot-a')
    assert slot is not None
    assert slot.state == PoolSlotState.CREATING
    assert slot.provider_resource_id is None
    assert slot.provider_create_intent_at is not None


async def test_counts_reflect_each_state_bucket(db_session):
    await _make_hot_slot(db_session, 'hot-1')
    await _make_hot_slot(db_session, 'hot-2')
    await insert_creating_slot(
        db_session,
        slot_id='inflight-1',
        backend='do-mars',
        sandbox_spec_id='do-mars-agent-server',
        config_id='cfg-1',
        intent_at=_utc_now(),
    )
    counts = await count_by_state(db_session, 'do-mars', 'do-mars-agent-server', 'cfg-1')
    assert counts.hot_ready == 2
    assert counts.in_flight == 1
    assert counts.total_inventory == 3


async def test_claim_requires_a_fresh_health_check(db_session):
    now = _utc_now()
    await _make_hot_slot(db_session, 'stale', health_checked_at=now - timedelta(minutes=30))
    await _make_hot_slot(db_session, 'fresh', health_checked_at=now)

    claimed = await claim_one(
        db_session,
        backend='do-mars',
        sandbox_spec_id='do-mars-agent-server',
        config_id='cfg-1',
        claim_owner_sandbox_id='conv-sandbox-1',
        claimed_by_user_id='user-1',
        claimed_org_id='org-1',
        min_health_check_after=now - timedelta(minutes=5),
    )
    assert claimed is not None
    assert claimed.id == 'fresh'


async def test_claim_never_selects_an_unhealthy_slot(db_session):
    now = _utc_now()
    await _make_hot_slot(db_session, 'bad', healthy=False, health_checked_at=now)

    claimed = await claim_one(
        db_session,
        backend='do-mars',
        sandbox_spec_id='do-mars-agent-server',
        config_id='cfg-1',
        claim_owner_sandbox_id='conv-sandbox-1',
        claimed_by_user_id='user-1',
        claimed_org_id='org-1',
        min_health_check_after=now - timedelta(minutes=5),
    )
    assert claimed is None


async def test_claim_states_defaults_to_hot_and_paused_preferring_hot(db_session):
    now = _utc_now()
    await insert_creating_slot(
        db_session,
        slot_id='paused-only',
        backend='do-mars',
        sandbox_spec_id='do-mars-agent-server',
        config_id='cfg-1',
        intent_at=now,
    )
    await set_state(
        db_session,
        'paused-only',
        PoolSlotState.PAUSED_RESERVE,
        provider_resource_id='provider-paused-only',
        last_health_ok=True,
    )
    from sqlalchemy import text as _text

    await db_session.execute(
        _text(
            'UPDATE do_mars_pool_slot SET last_health_check_at = :ts WHERE id = :id'
        ),
        {'ts': now, 'id': 'paused-only'},
    )
    await db_session.commit()

    # Default states: a paused-only pool is still claimable (the caller is
    # assumed able to resume it, e.g. the reconciler's own future use).
    claimed = await claim_one(
        db_session,
        backend='do-mars',
        sandbox_spec_id='do-mars-agent-server',
        config_id='cfg-1',
        claim_owner_sandbox_id='conv-sandbox-1',
        claimed_by_user_id='user-1',
        claimed_org_id='org-1',
        min_health_check_after=now - timedelta(minutes=5),
    )
    assert claimed is not None
    assert claimed.id == 'paused-only'
    assert claimed.state == PoolSlotState.CLAIMING


async def test_claim_states_hot_only_skips_a_paused_reserve_slot(db_session):
    now = _utc_now()
    await insert_creating_slot(
        db_session,
        slot_id='paused-only-2',
        backend='do-mars',
        sandbox_spec_id='do-mars-agent-server',
        config_id='cfg-1',
        intent_at=now,
    )
    await set_state(
        db_session,
        'paused-only-2',
        PoolSlotState.PAUSED_RESERVE,
        provider_resource_id='provider-paused-only-2',
        last_health_ok=True,
    )
    from sqlalchemy import text as _text

    await db_session.execute(
        _text(
            'UPDATE do_mars_pool_slot SET last_health_check_at = :ts WHERE id = :id'
        ),
        {'ts': now, 'id': 'paused-only-2'},
    )
    await db_session.commit()

    # A caller that cannot resume inline (DoMarsSandboxService.start_sandbox)
    # restricts to HOT_READY: the paused slot above must never be selected,
    # and must be left exactly as-is (still PAUSED_RESERVE, not CLAIMING) so
    # a plain create fallback never strands or double-claims it.
    claimed = await claim_one(
        db_session,
        backend='do-mars',
        sandbox_spec_id='do-mars-agent-server',
        config_id='cfg-1',
        claim_owner_sandbox_id='conv-sandbox-1',
        claimed_by_user_id='user-1',
        claimed_org_id='org-1',
        min_health_check_after=now - timedelta(minutes=5),
        states=(PoolSlotState.HOT_READY,),
    )
    assert claimed is None
    counts = await count_by_state(db_session, 'do-mars', 'do-mars-agent-server', 'cfg-1')
    assert counts.paused_reserve == 1
    assert counts.in_flight == 0

    await _make_hot_slot(db_session, 'hot-only', health_checked_at=now)
    claimed_hot = await claim_one(
        db_session,
        backend='do-mars',
        sandbox_spec_id='do-mars-agent-server',
        config_id='cfg-1',
        claim_owner_sandbox_id='conv-sandbox-2',
        claimed_by_user_id='user-2',
        claimed_org_id='org-2',
        min_health_check_after=now - timedelta(minutes=5),
        states=(PoolSlotState.HOT_READY,),
    )
    assert claimed_hot is not None
    assert claimed_hot.id == 'hot-only'


async def test_claim_is_exclusive_and_transaction_safe_across_sessions():
    """Two concurrent "app instances" racing the same pool never both win.

    Each half of the race uses its own AsyncSession/connection, exactly as
    two separate app or worker processes would, so this exercises the real
    cross-connection ``SELECT ... FOR UPDATE SKIP LOCKED`` behavior rather
    than Python-level locking.
    """

    import asyncio

    engine = create_async_engine(DSN)
    setup = AsyncSession(engine, expire_on_commit=False)
    now = _utc_now()
    await _make_hot_slot(setup, 'only-slot', health_checked_at=now)
    await setup.close()

    async def attempt(owner: str):
        session = AsyncSession(engine, expire_on_commit=False)
        try:
            return await claim_one(
                session,
                backend='do-mars',
                sandbox_spec_id='do-mars-agent-server',
                config_id='cfg-1',
                claim_owner_sandbox_id=owner,
                claimed_by_user_id='user-1',
                claimed_org_id='org-1',
                min_health_check_after=now - timedelta(minutes=5),
            )
        finally:
            await session.close()

    results = await asyncio.gather(*(attempt(f'conv-{i}') for i in range(8)))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert winners[0].id == 'only-slot'
    assert winners[0].claim_owner_sandbox_id in {f'conv-{i}' for i in range(8)}
    await engine.dispose()


async def test_claimed_slot_is_never_eligible_for_a_second_claim(db_session):
    now = _utc_now()
    await _make_hot_slot(db_session, 'slot-x', health_checked_at=now)
    first = await claim_one(
        db_session,
        backend='do-mars',
        sandbox_spec_id='do-mars-agent-server',
        config_id='cfg-1',
        claim_owner_sandbox_id='conv-a',
        claimed_by_user_id='user-a',
        claimed_org_id='org-a',
        min_health_check_after=now - timedelta(minutes=5),
    )
    assert first is not None

    second = await claim_one(
        db_session,
        backend='do-mars',
        sandbox_spec_id='do-mars-agent-server',
        config_id='cfg-1',
        claim_owner_sandbox_id='conv-b',
        claimed_by_user_id='user-b',
        claimed_org_id='org-b',
        min_health_check_after=now - timedelta(minutes=5),
    )
    assert second is None

    slot = await get_slot(db_session, 'slot-x')
    assert slot.state == PoolSlotState.CLAIMING
    assert slot.claim_owner_sandbox_id == 'conv-a'
    assert slot.claimed_by_user_id == 'user-a'


async def test_sweep_never_returns_claiming_or_claimed_rows(db_session):
    # claim_one takes the oldest eligible row first, so 'claimed-first' (the
    # one inserted earlier) is the one that gets claimed here.
    now = _utc_now()
    await _make_hot_slot(db_session, 'claimed-first', health_checked_at=now)
    await _make_hot_slot(db_session, 'still-unclaimed', health_checked_at=now)
    await claim_one(
        db_session,
        backend='do-mars',
        sandbox_spec_id='do-mars-agent-server',
        config_id='cfg-1',
        claim_owner_sandbox_id='conv-a',
        claimed_by_user_id='user-a',
        claimed_org_id='org-a',
        min_health_check_after=now - timedelta(minutes=5),
    )
    sweepable = await select_sweepable(
        db_session,
        'do-mars',
        'do-mars-agent-server',
        'cfg-1',
        (PoolSlotState.HOT_READY, PoolSlotState.PAUSED_RESERVE),
        limit=10,
    )
    ids = {s.id for s in sweepable}
    assert ids == {'still-unclaimed'}


async def test_delete_slot_removes_it(db_session):
    await _make_hot_slot(db_session, 'to-delete')
    await delete_slot(db_session, 'to-delete')
    assert await get_slot(db_session, 'to-delete') is None


async def _claim(db_session, slot_id_hint, owner='conv-owner'):
    now = _utc_now()
    await _make_hot_slot(db_session, slot_id_hint, health_checked_at=now)
    claimed = await claim_one(
        db_session,
        backend='do-mars',
        sandbox_spec_id='do-mars-agent-server',
        config_id='cfg-1',
        claim_owner_sandbox_id=owner,
        claimed_by_user_id='user-1',
        claimed_org_id='org-1',
        min_health_check_after=now - timedelta(minutes=5),
    )
    assert claimed is not None
    return claimed


async def test_promote_claim_inserts_app_row_and_deletes_pool_row_together(db_session):
    claimed = await _claim(db_session, 'promote-ok')

    async def create_row(session):
        await session.execute(
            fake_app_sandbox_table.insert().values(
                id='app-sandbox-1', provider_resource_id=claimed.provider_resource_id
            )
        )

    await promote_claim_to_sandbox(
        db_session,
        slot_id=claimed.id,
        claim_owner_sandbox_id='conv-owner',
        create_sandbox_row=create_row,
    )

    assert await get_slot(db_session, claimed.id) is None
    row = (
        await db_session.execute(
            fake_app_sandbox_table.select().where(
                fake_app_sandbox_table.c.id == 'app-sandbox-1'
            )
        )
    ).first()
    assert row is not None
    assert row.provider_resource_id == claimed.provider_resource_id


async def test_promote_claim_rolls_back_both_writes_on_failure(db_session):
    """If create_sandbox_row raises, neither the app row nor the slot
    deletion may persist: the slot must stay claimable-recoverable (still
    CLAIMING) so the reconciler's stale-claim path - not a half-applied
    write - is what eventually decides its fate.
    """

    claimed = await _claim(db_session, 'promote-fails')

    class _Boom(RuntimeError):
        pass

    async def create_row_that_fails(session):
        await session.execute(
            fake_app_sandbox_table.insert().values(
                id='app-sandbox-should-not-exist',
                provider_resource_id=claimed.provider_resource_id,
            )
        )
        raise _Boom('simulated crash before commit')

    with pytest.raises(_Boom):
        await promote_claim_to_sandbox(
            db_session,
            slot_id=claimed.id,
            claim_owner_sandbox_id='conv-owner',
            create_sandbox_row=create_row_that_fails,
        )

    # Use a fresh session/connection: the failed one may be in a rolled-back
    # but otherwise unusable state, and a fresh read is also the honest
    # check - it proves durability, not just in-memory session state.
    engine = create_async_engine(DSN)
    async with AsyncSession(engine, expire_on_commit=False) as fresh:
        slot = await get_slot(fresh, claimed.id)
        assert slot is not None
        assert slot.state == PoolSlotState.CLAIMING
        app_row = (
            await fresh.execute(
                fake_app_sandbox_table.select().where(
                    fake_app_sandbox_table.c.id == 'app-sandbox-should-not-exist'
                )
            )
        ).first()
        assert app_row is None
    await engine.dispose()


async def test_promote_claim_rejects_wrong_owner(db_session):
    claimed = await _claim(db_session, 'promote-wrong-owner')

    async def create_row(session):
        pytest.fail('must not be called when the owner check fails')

    with pytest.raises(ClaimPromotionError):
        await promote_claim_to_sandbox(
            db_session,
            slot_id=claimed.id,
            claim_owner_sandbox_id='someone-else',
            create_sandbox_row=create_row,
        )
    slot = await get_slot(db_session, claimed.id)
    assert slot is not None
    assert slot.state == PoolSlotState.CLAIMING


async def test_recovery_lock_skips_a_slot_whose_promotion_is_in_flight():
    """Exercises the real production functions on both sides of the race
    (not a reimplementation of either): ``promote_claim_to_sandbox`` holding
    its FOR UPDATE lock mid-promotion must make
    ``lock_stale_claiming_slot_for_recovery`` return None immediately for
    the same row from a second, fully independent connection.

    Without ``SKIP LOCKED``, a plain ``DELETE`` by an unlocked-scan-based
    recovery path would simply block on Postgres's own row lock until the
    promotion's transaction ends - a real liveness bug (the reconciler
    tick hangs, or times out and raises, on every tick that races a
    promotion), confirmed by hand against the same fixtures this test uses:
    a pre-fix unlocked `select_stale_claiming` + plain `delete_slot` pairing
    deadlocks here instead of corrupting anything, because Postgres's own
    locking already forbids a concurrent DELETE. ``SKIP LOCKED`` turns that
    hang into an immediate, correct no-op, which is what makes "run the
    recovery pass every tick without risking a stuck reconciler" possible.
    The no-corruption property is proven separately by
    test_promote_claim_rolls_back_both_writes_on_failure, which is the
    scenario that actually matters for a lost/crashed claimer: the
    sequential crash-between-two-commits window, not a live race.
    """

    import asyncio

    engine = create_async_engine(DSN)
    setup = AsyncSession(engine, expire_on_commit=False)
    claimed_id = (await _claim(setup, 'race-slot')).id
    await setup.close()

    deadline = _utc_now() + timedelta(seconds=1)  # already "stale" by any threshold
    promote_started = asyncio.Event()
    allow_promote_to_finish = asyncio.Event()

    async def slow_create_row(session):
        promote_started.set()
        # Hold promote_claim_to_sandbox's FOR UPDATE lock open while a
        # concurrent recovery attempt runs against the same row from a
        # second connection.
        await asyncio.wait_for(allow_promote_to_finish.wait(), timeout=5)
        await session.execute(
            fake_app_sandbox_table.insert().values(
                id='race-app-sandbox', provider_resource_id='provider-race-slot'
            )
        )

    async def claimer():
        session = AsyncSession(engine, expire_on_commit=False)
        try:
            await promote_claim_to_sandbox(
                session,
                slot_id=claimed_id,
                claim_owner_sandbox_id='conv-owner',
                create_sandbox_row=slow_create_row,
            )
            return 'promoted'
        finally:
            await session.close()

    async def recovery_attempt():
        await promote_started.wait()
        session = AsyncSession(engine, expire_on_commit=False)
        try:
            locked = await lock_stale_claiming_slot_for_recovery(
                session,
                backend='do-mars',
                sandbox_spec_id='do-mars-agent-server',
                config_id='cfg-1',
                slot_id=claimed_id,
                older_than=deadline,
            )
            if locked is None:
                await session.rollback()
                return 'skipped'
            # If this ever fires, the production lock function failed to
            # exclude an in-flight promotion - that would itself be the bug
            # under test, so destroy for a clear, loud assertion failure.
            await delete_slot(session, claimed_id)
            return 'destroyed'
        finally:
            await session.close()
            allow_promote_to_finish.set()

    results = await asyncio.gather(claimer(), recovery_attempt())
    await engine.dispose()

    assert results == ['promoted', 'skipped'], (
        f'expected the in-flight promotion to win and recovery to skip, got {results}'
    )
    # And the promotion's own effects are the ones that persisted.
    engine2 = create_async_engine(DSN)
    async with AsyncSession(engine2, expire_on_commit=False) as fresh:
        assert await get_slot(fresh, claimed_id) is None
        app_row = (
            await fresh.execute(
                fake_app_sandbox_table.select().where(
                    fake_app_sandbox_table.c.id == 'race-app-sandbox'
                )
            )
        ).first()
        assert app_row is not None
    await engine2.dispose()


async def test_recovery_lock_destroys_only_when_genuinely_idle_and_stale(db_session):
    claimed = await _claim(db_session, 'genuinely-stale')
    deadline = _utc_now() + timedelta(seconds=1)

    locked = await lock_stale_claiming_slot_for_recovery(
        db_session,
        backend='do-mars',
        sandbox_spec_id='do-mars-agent-server',
        config_id='cfg-1',
        slot_id=claimed.id,
        older_than=deadline,
    )
    assert locked is not None
    assert locked.id == claimed.id
    await delete_slot(db_session, claimed.id)
    assert await get_slot(db_session, claimed.id) is None


async def test_recovery_lock_ignores_a_slot_not_yet_past_the_deadline(db_session):
    claimed = await _claim(db_session, 'not-stale-yet')
    # older_than in the past: the slot's state_changed_at (just now) is not
    # older than that, so it must not be treated as recoverable.
    not_yet = _utc_now() - timedelta(seconds=1000)

    locked = await lock_stale_claiming_slot_for_recovery(
        db_session,
        backend='do-mars',
        sandbox_spec_id='do-mars-agent-server',
        config_id='cfg-1',
        slot_id=claimed.id,
        older_than=not_yet,
    )
    assert locked is None
    slot = await get_slot(db_session, claimed.id)
    assert slot is not None
    assert slot.state == PoolSlotState.CLAIMING
