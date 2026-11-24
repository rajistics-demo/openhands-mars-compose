"""Reconciler business-logic tests: real PostgreSQL, a fully synthetic
``PoolProvider`` fake - no real DigitalOcean/network dependency, consistent
with this validation being a local functional check, not a live-MARS test.

Uses the same disposable PostgreSQL the store tests use; see
test_do_mars_pool_store.py's module docstring for how to point this at a
different instance.
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from openhands.app_server.sandbox.do_mars_pool_config import DoMarsPoolConfig
from openhands.app_server.sandbox.do_mars_pool_models import PoolSlotState
from openhands.app_server.sandbox.do_mars_pool_reconciler import DoMarsPoolReconciler
from openhands.app_server.sandbox.do_mars_pool_store import (
    count_by_state,
    insert_creating_slot,
    pool_slot_table,
    set_state,
)

pytestmark = pytest.mark.asyncio

DSN = os.environ.get(
    'MARS_POOL_TEST_DSN',
    'postgresql+asyncpg://openhands:openhands@localhost:15432/openhands',
)
BACKEND = 'do-mars'
SPEC_ID = 'do-mars-agent-server'
CONFIG_ID = 'cfg-1'


@pytest.fixture(autouse=True)
async def _clean_table():
    engine = create_async_engine(DSN)
    async with engine.begin() as conn:
        await conn.execute(text('TRUNCATE TABLE do_mars_pool_slot'))
    await engine.dispose()
    yield


@dataclass
class FakeSession:
    """Duck-typed stand-in for DoMarsSession: the reconciler only ever
    reads ``.id`` off a provider's create_session() result, so the fake
    does not need the real dataclass's extra fields."""

    id: str


@dataclass
class FakePoolProvider:
    """Deterministic, in-memory stand-in for DoMarsPoolProviderAdapter.

    Every call is recorded so tests can assert what was, and crucially what
    was *not*, called - e.g. that a paused slot's health check never calls
    probe_alive/probe_dormant.
    """

    created: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    paused: list[str] = field(default_factory=list)
    resumed: list[str] = field(default_factory=list)
    probed_alive: list[str] = field(default_factory=list)
    probed_dormant: list[str] = field(default_factory=list)
    status_checked: list[str] = field(default_factory=list)

    dormant_result: bool = True
    alive_result: bool = True
    paused_confirm_result: bool = True
    create_should_fail: bool = False
    _paused_ids: set = field(default_factory=set)

    async def create_session(self, name: str, config_id: str) -> FakeSession:
        if self.create_should_fail:
            raise RuntimeError('simulated provider create failure')
        provider_id = f'provider-{name}'
        self.created.append(provider_id)
        return FakeSession(id=provider_id)

    async def get_session(self, session_id: str):
        return FakeSession(id=session_id)

    async def list_sessions_by_name(self, name: str):
        return []

    async def pause_session(self, session_id: str) -> None:
        self.paused.append(session_id)
        self._paused_ids.add(session_id)

    async def resume_session(self, session_id: str) -> None:
        self.resumed.append(session_id)
        self._paused_ids.discard(session_id)

    async def delete_session(self, session_id: str) -> None:
        self.deleted.append(session_id)

    async def probe_dormant(self, session_id: str) -> bool:
        self.probed_dormant.append(session_id)
        return self.dormant_result

    async def probe_alive(self, session_id: str) -> bool:
        self.probed_alive.append(session_id)
        return self.alive_result

    async def is_still_paused(self, session_id: str) -> bool:
        self.status_checked.append(session_id)
        return self.paused_confirm_result and session_id in self._paused_ids


def make_config(**overrides) -> DoMarsPoolConfig:
    params = dict(
        enabled=True,
        sandbox_spec_id=SPEC_ID,
        min_hot=1,
        min_paused_reserve=0,
        max_total=1,
    )
    params.update(overrides)
    return DoMarsPoolConfig(**params)


def make_reconciler(provider, config, *, now=None) -> DoMarsPoolReconciler:
    engine = create_async_engine(DSN)
    session_factory = lambda: AsyncSession(engine, expire_on_commit=False)  # noqa: E731
    return DoMarsPoolReconciler(
        session_factory=session_factory,
        provider=provider,
        config=config,
        backend=BACKEND,
        config_id=CONFIG_ID,
        now=now or (lambda: datetime.now(timezone.utc)),
    )


async def _seed_slot(slot_id: str, state: PoolSlotState, *, provider_resource_id: str):
    """Fast, direct test setup for a state _replenish_and_rebalance's own
    create/promote path cannot organically reach on its own (e.g. more hot
    slots than min_hot - that only happens in reality via an operator
    lowering min_hot after slots already exist, not via one tick's logic).
    """

    engine = create_async_engine(DSN)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        now = datetime.now(timezone.utc)
        await insert_creating_slot(
            session,
            slot_id=slot_id,
            backend=BACKEND,
            sandbox_spec_id=SPEC_ID,
            config_id=CONFIG_ID,
            intent_at=now,
        )
        await set_state(
            session,
            slot_id,
            state,
            provider_resource_id=provider_resource_id,
            last_health_ok=True,
        )
    await engine.dispose()


async def _counts():
    engine = create_async_engine(DSN)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        counts = await count_by_state(session, BACKEND, SPEC_ID, CONFIG_ID)
    await engine.dispose()
    return counts


async def test_disabled_pool_does_not_tick():
    provider = FakePoolProvider()
    reconciler = make_reconciler(provider, make_config(enabled=False))
    report = await reconciler.run_once()
    assert report.ticked is False
    assert report.skip_reason == 'pool_disabled'
    assert provider.created == []


async def test_empty_pool_creates_toward_min_hot():
    provider = FakePoolProvider()
    reconciler = make_reconciler(provider, make_config(min_hot=1, max_total=1))
    report = await reconciler.run_once()
    assert report.created == 1
    assert provider.created and provider.probed_dormant
    counts = await _counts()
    assert counts.hot_ready == 1
    assert counts.total_inventory == 1


async def test_create_failure_marks_error_not_silently_dropped():
    provider = FakePoolProvider(create_should_fail=True)
    reconciler = make_reconciler(provider, make_config(min_hot=1, max_total=1))
    report = await reconciler.run_once()
    assert report.boot_failed == 1
    counts = await _counts()
    assert counts.hot_ready == 0
    assert counts.error == 1


async def test_excess_hot_is_demoted_toward_paused_reserve():
    # min_hot=1 but two HOT_READY slots already exist - the shape an
    # operator sees right after lowering min_hot from 2 to 1, since
    # _replenish_and_rebalance's own create path never overshoots min_hot
    # on its own.
    await _seed_slot('hot-1', PoolSlotState.HOT_READY, provider_resource_id='provider-hot-1')
    await _seed_slot('hot-2', PoolSlotState.HOT_READY, provider_resource_id='provider-hot-2')

    provider = FakePoolProvider()
    config = make_config(min_hot=1, min_paused_reserve=1, max_total=2)
    reconciler = make_reconciler(provider, config)
    report = await reconciler.run_once()
    assert report.paused_for_demotion == 1
    assert provider.paused
    counts = await _counts()
    assert counts.hot_ready == 1
    assert counts.paused_reserve == 1


async def test_paused_reserve_is_promoted_to_hot_when_short():
    # hot=0, paused=1: below min_hot with reserve available, so promotion
    # must be preferred over paying for a brand-new create.
    await _seed_slot(
        'paused-1', PoolSlotState.PAUSED_RESERVE, provider_resource_id='provider-paused-1'
    )

    provider = FakePoolProvider()
    config = make_config(min_hot=1, min_paused_reserve=0, max_total=1)
    reconciler = make_reconciler(provider, config)
    report = await reconciler.run_once()
    assert report.resumed_for_promotion == 1
    assert provider.resumed == ['provider-paused-1']
    assert provider.created == []  # must prefer promotion over a new create
    counts = await _counts()
    assert counts.hot_ready == 1
    assert counts.paused_reserve == 0


def _an_hour_from_now():
    """Forces _due_for_health_check to treat every slot as due, regardless
    of health_check_interval_seconds, without flaking on real elapsed wall
    time or reaching into internal row timestamps directly."""

    return datetime.now(timezone.utc) + timedelta(hours=1)


async def test_health_check_reaps_an_unhealthy_hot_slot():
    # Create one hot slot first, with replenishment on.
    await make_reconciler(FakePoolProvider(), make_config(min_hot=1, max_total=1)).run_once()
    counts = await _counts()
    assert counts.hot_ready == 1

    # Tick 1 (replenish off): provider reports the slot as no longer alive.
    # _health_check_hot only ever marks last_health_ok=False; _reap (which
    # runs *before* the health check within a tick) is what actually
    # destroys it, so that happens on the *next* tick, not this one.
    unhealthy_config = make_config(min_hot=1, max_total=1, replenish_enabled=False)
    provider2 = FakePoolProvider(alive_result=False)
    reconciler2 = make_reconciler(provider2, unhealthy_config, now=_an_hour_from_now)
    report1 = await reconciler2.run_once()
    assert report1.health_checked_hot == 1
    assert report1.reaped == 0
    counts = await _counts()
    assert counts.hot_ready == 1  # still present, just marked unhealthy

    report2 = await reconciler2.run_once()
    assert report2.reaped == 1
    assert provider2.deleted
    counts = await _counts()
    assert counts.hot_ready == 0
    assert counts.error == 0  # reaped (deleted), not left as an ERROR row


async def test_health_check_never_dials_a_paused_slot():
    """Paused slots are control-plane-status-only checks: the pilot/Canvas
    design explicitly forbids dialing (probe_alive/probe_dormant) a paused
    VM just to check on it, since that could itself wake it.
    """

    await _seed_slot(
        'paused-1', PoolSlotState.PAUSED_RESERVE, provider_resource_id='provider-paused-1'
    )
    counts = await _counts()
    assert counts.paused_reserve == 1

    provider = FakePoolProvider()
    config = make_config(min_hot=0, min_paused_reserve=1, max_total=1, replenish_enabled=False)
    # Fresh now() far enough ahead that _due_for_health_check treats the
    # seeded paused slot as due, without sleeping in the test or reaching
    # into row timestamps directly.
    checker = make_reconciler(provider, config, now=_an_hour_from_now)
    await checker.run_once()
    assert provider.probed_alive == []
    assert provider.probed_dormant == []
    assert provider.status_checked  # it did check paused status via is_still_paused


async def test_run_once_recovers_a_genuinely_stuck_claim_end_to_end():
    """A crashed claimer (no concurrent promotion in flight - that case is
    covered at the store layer by test_recovery_lock_skips_a_slot_whose_
    promotion_is_in_flight) gets destroyed by a plain run_once() tick, end
    to end through the real recover_stuck_claims code path.
    """

    provider = FakePoolProvider()
    config = make_config(min_hot=0, max_total=1, replenish_enabled=False, claim_timeout_seconds=1)
    engine = create_async_engine(DSN)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        now = datetime.now(timezone.utc)
        await insert_creating_slot(
            session,
            slot_id='stuck-claim',
            backend=BACKEND,
            sandbox_spec_id=SPEC_ID,
            config_id=CONFIG_ID,
            intent_at=now,
        )
        await set_state(
            session,
            'stuck-claim',
            PoolSlotState.HOT_READY,
            provider_resource_id='provider-stuck-claim',
            last_health_ok=True,
        )
        await session.execute(
            pool_slot_table.update()
            .where(pool_slot_table.c.id == 'stuck-claim')
            .values(
                state=PoolSlotState.CLAIMING.value,
                claim_owner_sandbox_id='crashed-owner',
                claimed_by_user_id='u',
                claimed_org_id='o',
            )
        )
        await session.commit()
    await engine.dispose()

    reconciler = make_reconciler(provider, config, now=_an_hour_from_now)
    report = await reconciler.run_once()
    assert report.recovered_stuck_claims == 1
    assert provider.deleted == ['provider-stuck-claim']
    counts = await _counts()
    assert counts.total_inventory == 0


async def test_concurrent_reconciler_ticks_are_mutually_exclusive():
    """Two app/worker instances ticking the same (backend, spec, config) at
    the same moment must not both run the same tick's actions - that is
    the one job of the per-tick advisory lock.
    """

    import asyncio

    config = make_config(min_hot=1, max_total=1)
    r1 = make_reconciler(FakePoolProvider(), config)
    r2 = make_reconciler(FakePoolProvider(), config)
    report1, report2 = await asyncio.gather(r1.run_once(), r2.run_once())

    ticked = [r for r in (report1, report2) if r.ticked]
    skipped = [r for r in (report1, report2) if not r.ticked]
    assert len(ticked) == 1
    assert len(skipped) == 1
    assert skipped[0].skip_reason == 'tick_in_progress_elsewhere'
