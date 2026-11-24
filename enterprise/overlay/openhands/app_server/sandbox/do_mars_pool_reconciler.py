"""The reconciliation loop for the opt-in DO MARS warm pool.

One tick (``run_once``) does, in order: recover stuck claims, reap
unhealthy/stale/error inventory, health-check hot slots (safe to dial - they
are already running and unclaimed), health-check paused slots (control-plane
status only - never dialed, see do_mars_pool_provider.PoolProvider), then
replenish/rebalance toward the configured hot and paused-reserve targets.

Every step is scoped to one (backend, sandbox_spec_id, config_id) triple and
every state-changing action commits its own short transaction, so a crash
between ticks or between two of these steps never leaves more than one
command's worth of work ambiguous, and the next tick's snapshot-based
decisions are always conservative (re-check, never assume).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.app_server.sandbox.do_mars_pool_config import DoMarsPoolConfig
from openhands.app_server.sandbox.do_mars_pool_models import PoolSlotState
from openhands.app_server.sandbox.do_mars_pool_provider import PoolProvider
from openhands.app_server.sandbox.do_mars_pool_store import (
    count_by_state,
    delete_slot,
    insert_creating_slot,
    lock_stale_claiming_slot_for_recovery,
    select_stale_claiming,
    select_sweepable,
    set_state,
)

logger = logging.getLogger(__name__)


@dataclass
class PoolTickReport:
    """What one ``run_once`` call did; for logging/tests, not persisted."""

    ticked: bool = True
    skip_reason: str | None = None
    recovered_stuck_claims: int = 0
    reaped: int = 0
    health_checked_hot: int = 0
    health_checked_paused: int = 0
    created: int = 0
    boot_failed: int = 0
    paused_for_demotion: int = 0
    resumed_for_promotion: int = 0
    errors: list[str] = field(default_factory=list)


def _advisory_lock_key(backend: str, sandbox_spec_id: str, config_id: str) -> int:
    """A stable (not Python's randomized ``hash()``) lock key.

    Every app/worker process must derive the *same* integer for the same
    spec/config, or the advisory lock never actually contends across
    processes - the one thing it exists for. sha256 is used only as a fast,
    stable, well-distributed digest, not for any security property.
    """

    digest = f'do-mars-pool:{backend}:{sandbox_spec_id}:{config_id}'.encode()
    # Postgres advisory lock keys are signed 64-bit; keep well inside that.
    return int.from_bytes(hashlib.sha256(digest).digest()[:7], 'big')


async def _try_tick_lock(db_session: AsyncSession, key: int) -> bool:
    """Non-blocking: true if this process now owns the tick, else false.

    ``pg_try_advisory_lock`` (session-scoped, not transaction-scoped) is used
    instead of the ``_xact`` variant so the lock is held for the whole tick
    across this function's several commits, then explicitly released at the
    end - never silently dropped at an intermediate commit.
    """

    return bool(
        (await db_session.execute(select(func.pg_try_advisory_lock(key)))).scalar()
    )


async def _unlock(db_session: AsyncSession, key: int) -> None:
    await db_session.execute(select(func.pg_advisory_unlock(key)))


@dataclass
class DoMarsPoolReconciler:
    """One tick of pool upkeep for one (backend, spec, config) triple.

    ``session_factory`` must return a *new* ``AsyncSession`` per call (e.g.
    the Enterprise ``a_session_maker``), because several steps each need
    their own short-lived transaction; the reconciler never holds one
    session open across the whole tick except the dedicated lock session.
    """

    session_factory: Callable[[], AsyncSession]
    provider: PoolProvider
    config: DoMarsPoolConfig
    backend: str
    config_id: str
    now: Callable[[], datetime] = datetime.utcnow

    async def run_once(self) -> PoolTickReport:
        if not self.config.enabled:
            return PoolTickReport(ticked=False, skip_reason='pool_disabled')
        assert self.config.sandbox_spec_id is not None
        key = _advisory_lock_key(self.backend, self.config.sandbox_spec_id, self.config_id)
        lock_session = self.session_factory()
        try:
            if not await _try_tick_lock(lock_session, key):
                return PoolTickReport(ticked=False, skip_reason='tick_in_progress_elsewhere')
            report = PoolTickReport()
            try:
                await self._recover_stuck_claims(report)
                await self._reap(report)
                await self._health_check_hot(report)
                await self._health_check_paused(report)
                if self.config.replenish_enabled:
                    await self._replenish_and_rebalance(report)
            except Exception as exc:  # noqa: BLE001 - one tick's fault must not crash the loop
                logger.exception('do_mars_pool.tick_failed')
                report.errors.append(str(exc))
            return report
        finally:
            await _unlock(lock_session, key)
            await lock_session.close()

    async def _recover_stuck_claims(self, report: PoolTickReport) -> None:
        """A CLAIMING row past the claim timeout means its claimer crashed or
        hung between locking the slot and finishing (or giving up on)
        promotion. The slot was touched for a specific tenant and its true
        state is unknown, so it is destroyed, never re-armed as hot/paused
        inventory - see the ERROR-state contract in do_mars_pool_models.

        ``select_stale_claiming`` below is only a cheap unlocked scan to
        find *candidate* ids. Before destroying any of them, each is
        re-locked and re-confirmed with
        ``lock_stale_claiming_slot_for_recovery`` on its own fresh
        session/transaction: that call uses ``FOR UPDATE SKIP LOCKED``
        against the same row ``promote_claim_to_sandbox`` locks during a
        real promotion, so a candidate whose promotion is genuinely
        in-flight is simply skipped this tick (never blocked on, never
        destroyed), and a candidate that already finished promoting (row
        now gone) or recovered on its own (row no longer CLAIMING) is also
        a no-op here. Only a candidate this reconciler itself manages to
        lock, with the lock showing it is still CLAIMING and still past the
        deadline, is ever destroyed - which is then provably, not just
        probably, safe: holding that lock is exactly what a concurrent
        promotion would need, and cannot have, at that moment.
        """

        deadline = self.now() - timedelta(seconds=self.config.claim_timeout_seconds)
        scan_session = self.session_factory()
        try:
            candidate_ids = [
                slot.id
                for slot in await select_stale_claiming(scan_session, self.backend, deadline)
            ]
        finally:
            await scan_session.close()

        for slot_id in candidate_ids:
            session = self.session_factory()
            try:
                locked = await lock_stale_claiming_slot_for_recovery(
                    session, backend=self.backend, slot_id=slot_id, older_than=deadline
                )
                if locked is None:
                    # Either a promotion is concurrently holding this row's
                    # lock, or the row has already left CLAIMING (promoted
                    # or recovered) since the scan above. Either way,
                    # nothing to do this tick.
                    await session.rollback()
                    continue
                await self._destroy(session, locked.id, locked.provider_resource_id)
                report.recovered_stuck_claims += 1
            finally:
                await session.close()

    async def _destroy(
        self, session: AsyncSession, slot_id: str, provider_resource_id: str | None
    ) -> None:
        """Best-effort provider delete, then always drop the row.

        Row deletion is unconditional even if the provider delete call
        fails: a slot that is ERROR/stuck/stale must stop being visible as
        pool inventory immediately, and is never claimable again regardless
        of provider-side cleanup outcome. A provider delete failure here is
        the same "needs operator reconciliation" situation as a non-pool
        sandbox's unresolved delete (see do_mars_sandbox_service); it is
        logged, not retried in place, because retrying delete against an
        unknown provider error risks masking a real leak rather than
        surfacing it.
        """

        if provider_resource_id:
            try:
                await self.provider.delete_session(provider_resource_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    'do_mars_pool.destroy_provider_delete_failed',
                    extra={'slot_id': slot_id, 'error': str(exc)},
                )
        await delete_slot(session, slot_id)

    async def _reap(self, report: PoolTickReport) -> None:
        """Remove ERROR rows and anything past the max age bound.

        Age and ERROR are checked here, independent of the health-check
        steps, so a slot that is simply too old to trust is reaped even if
        its last health probe was fine a moment ago.
        """

        assert self.config.sandbox_spec_id is not None
        session = self.session_factory()
        try:
            too_old = self.now() - timedelta(seconds=self.config.max_slot_age_seconds)
            candidates = await select_sweepable(
                session,
                self.backend,
                self.config.sandbox_spec_id,
                self.config_id,
                (PoolSlotState.HOT_READY, PoolSlotState.PAUSED_RESERVE, PoolSlotState.ERROR),
                limit=50,
            )
            for slot in candidates:
                unhealthy = slot.last_health_ok is False
                stale = slot.created_at < too_old
                is_error = slot.state == PoolSlotState.ERROR
                if is_error or unhealthy or stale:
                    await self._destroy(session, slot.id, slot.provider_resource_id)
                    report.reaped += 1
        finally:
            await session.close()

    async def _due_for_health_check(self, slot, interval_seconds: int) -> bool:
        if slot.last_health_check_at is None:
            return True
        return slot.last_health_check_at < self.now() - timedelta(seconds=interval_seconds)

    async def _health_check_hot(self, report: PoolTickReport) -> None:
        """Dial each due HOT_READY slot's dormant-init probe.

        Dialing is safe here specifically because HOT_READY means: running,
        unclaimed, and never yet handed tenant secrets - the same guarantee
        ``do_mars_sandbox_service`` relies on for its own boot-time dormant
        probe. A probe failure sets ``last_health_ok=False`` so the next
        ``_reap`` removes it; it is never deleted from inside this loop, to
        keep each step single-purpose and its own commit boundary small.
        """

        assert self.config.sandbox_spec_id is not None
        session = self.session_factory()
        try:
            candidates = await select_sweepable(
                session,
                self.backend,
                self.config.sandbox_spec_id,
                self.config_id,
                (PoolSlotState.HOT_READY,),
                limit=20,
            )
            for slot in candidates:
                if not await self._due_for_health_check(
                    slot, self.config.health_check_interval_seconds
                ):
                    continue
                if not slot.provider_resource_id:
                    continue
                try:
                    ok = await self.provider.probe_alive(slot.provider_resource_id)
                except Exception:  # noqa: BLE001
                    ok = False
                await set_state(session, slot.id, PoolSlotState.HOT_READY, last_health_ok=ok)
                report.health_checked_hot += 1
        finally:
            await session.close()

    async def _health_check_paused(self, report: PoolTickReport) -> None:
        """Confirm each due PAUSED_RESERVE slot via control-plane status only.

        Never calls ``probe_alive``/``probe_dormant`` here: a port-forward
        dial to a paused MARS VM can itself trigger the provider to resume
        it, which would silently burn a paused slot's "pause" the moment the
        pool merely checked on it. ``is_still_paused`` must be implemented
        as a status read, never a tunnel dial - see PoolProvider's contract.
        """

        assert self.config.sandbox_spec_id is not None
        session = self.session_factory()
        try:
            candidates = await select_sweepable(
                session,
                self.backend,
                self.config.sandbox_spec_id,
                self.config_id,
                (PoolSlotState.PAUSED_RESERVE,),
                limit=20,
            )
            for slot in candidates:
                if not await self._due_for_health_check(
                    slot, self.config.health_check_interval_seconds
                ):
                    continue
                if not slot.provider_resource_id:
                    continue
                try:
                    ok = await self.provider.is_still_paused(slot.provider_resource_id)
                except Exception:  # noqa: BLE001
                    ok = False
                await set_state(
                    session, slot.id, PoolSlotState.PAUSED_RESERVE, last_health_ok=ok
                )
                report.health_checked_paused += 1
        finally:
            await session.close()

    async def _replenish_and_rebalance(self, report: PoolTickReport) -> None:
        """Move one step toward (hot == min_hot, paused == min_paused_reserve),
        bounded by max_total and by this tick's per-action caps.

        Reads one ``PoolCounts`` snapshot, then takes at most one kind of
        action from it (create, promote, or demote) - never more than one
        category per tick - so a single tick's worth of provider calls stays
        small and predictable, and the next tick re-reads real state rather
        than compounding an in-memory plan across several provider calls.
        """

        assert self.config.sandbox_spec_id is not None
        session = self.session_factory()
        try:
            counts = await count_by_state(
                session, self.backend, self.config.sandbox_spec_id, self.config_id
            )
        finally:
            await session.close()

        if counts.hot_ready > self.config.min_hot and counts.paused_reserve < (
            self.config.min_paused_reserve
        ):
            await self._demote_excess_hot(report, counts)
            return

        if counts.hot_ready < self.config.min_hot:
            if counts.paused_reserve > 0:
                # Promotion only moves a slot between buckets (paused_reserve
                # -> in_flight(RESUMING) -> hot_ready); total_inventory is
                # unchanged throughout, so it must never be gated by the
                # same max_total headroom check a brand-new create needs -
                # otherwise a pool sitting exactly at max_total with spare
                # paused reserve could never rebalance back to hot.
                await self._promote_one_paused(report)
                return
            if counts.total_inventory < self.config.max_total:
                await self._create_one(report)
                return

        if counts.total_inventory > self.config.max_total:
            await self._shed_excess(report, counts)

    async def _create_one(self, report: PoolTickReport) -> None:
        """Mirrors the non-pool create-intent pattern: the CREATING row
        commits before any provider call, so a crash right after the
        provider accepts the request still leaves a discoverable row - see
        ``do_mars_pool_store.insert_creating_slot``.
        """

        assert self.config.sandbox_spec_id is not None
        slot_id = f'pool-{self.config.sandbox_spec_id}-{secrets.token_hex(8)}'
        session = self.session_factory()
        try:
            await insert_creating_slot(
                session,
                slot_id=slot_id,
                backend=self.backend,
                sandbox_spec_id=self.config.sandbox_spec_id,
                config_id=self.config_id,
                intent_at=self.now(),
            )
        finally:
            await session.close()

        session = self.session_factory()
        try:
            try:
                created = await self.provider.create_session(slot_id, self.config_id)
            except Exception as exc:  # noqa: BLE001
                await set_state(
                    session, slot_id, PoolSlotState.ERROR, error_detail=str(exc)[:500]
                )
                report.boot_failed += 1
                return

            await set_state(
                session, slot_id, PoolSlotState.BOOTING, provider_resource_id=created.id
            )
            try:
                ready = await self.provider.probe_dormant(created.id)
            except Exception:  # noqa: BLE001
                ready = False
            if ready:
                await set_state(session, slot_id, PoolSlotState.HOT_READY, last_health_ok=True)
                report.created += 1
            else:
                # Boot did not confirm dormant-ready within this tick. Left
                # in BOOTING rather than marked ERROR immediately: a slow
                # but otherwise fine boot gets another look on the next
                # tick. Age-based reaping (via max_slot_age_seconds, checked
                # against created_at) is what eventually reclaims a VM that
                # never finishes booting - BOOTING rows are intentionally
                # not included in select_sweepable's states today, so a
                # stuck BOOTING row is a known gap; see STATUS.md.
                report.boot_failed += 1
        finally:
            await session.close()

    async def _promote_one_paused(self, report: PoolTickReport) -> None:
        assert self.config.sandbox_spec_id is not None
        session = self.session_factory()
        try:
            candidates = await select_sweepable(
                session,
                self.backend,
                self.config.sandbox_spec_id,
                self.config_id,
                (PoolSlotState.PAUSED_RESERVE,),
                limit=1,
            )
            if not candidates:
                return
            slot = candidates[0]
            await set_state(session, slot.id, PoolSlotState.RESUMING)
        finally:
            await session.close()

        session = self.session_factory()
        try:
            try:
                await self.provider.resume_session(slot.provider_resource_id)
                ok = await self.provider.probe_alive(slot.provider_resource_id)
            except Exception:  # noqa: BLE001
                ok = False
            if ok:
                await set_state(session, slot.id, PoolSlotState.HOT_READY, last_health_ok=True)
                report.resumed_for_promotion += 1
            else:
                await set_state(
                    session,
                    slot.id,
                    PoolSlotState.ERROR,
                    error_detail='resume did not reach a dormant-ready state',
                )
        finally:
            await session.close()

    async def _demote_excess_hot(self, report: PoolTickReport, counts) -> None:
        assert self.config.sandbox_spec_id is not None
        budget = min(
            self.config.max_concurrent_pauses_per_tick,
            counts.hot_ready - self.config.min_hot,
            self.config.min_paused_reserve - counts.paused_reserve,
        )
        if budget <= 0:
            return
        session = self.session_factory()
        try:
            candidates = await select_sweepable(
                session,
                self.backend,
                self.config.sandbox_spec_id,
                self.config_id,
                (PoolSlotState.HOT_READY,),
                limit=budget,
            )
        finally:
            await session.close()

        for slot in candidates:
            session = self.session_factory()
            try:
                await set_state(session, slot.id, PoolSlotState.PAUSING)
            finally:
                await session.close()
            session = self.session_factory()
            try:
                try:
                    await self.provider.pause_session(slot.provider_resource_id)
                    ok = await self.provider.is_still_paused(slot.provider_resource_id)
                except Exception:  # noqa: BLE001
                    ok = False
                if ok:
                    await set_state(
                        session, slot.id, PoolSlotState.PAUSED_RESERVE, last_health_ok=True
                    )
                    report.paused_for_demotion += 1
                else:
                    await set_state(
                        session,
                        slot.id,
                        PoolSlotState.ERROR,
                        error_detail='pause did not confirm a paused state',
                    )
            finally:
                await session.close()

    async def _shed_excess(self, report: PoolTickReport, counts) -> None:
        """Over max_total with nothing to promote/demote toward the floors:
        delete the oldest unclaimed excess outright, cheapest-first (ERROR,
        then PAUSED_RESERVE, then HOT_READY), rather than paying to keep
        idle capacity the operator's own cap says is too much.
        """

        assert self.config.sandbox_spec_id is not None
        over_by = counts.total_inventory - self.config.max_total
        for states in (
            (PoolSlotState.ERROR,),
            (PoolSlotState.PAUSED_RESERVE,),
            (PoolSlotState.HOT_READY,),
        ):
            if over_by <= 0:
                break
            session = self.session_factory()
            try:
                candidates = await select_sweepable(
                    session,
                    self.backend,
                    self.config.sandbox_spec_id,
                    self.config_id,
                    states,
                    limit=over_by,
                )
                for slot in candidates:
                    await self._destroy(session, slot.id, slot.provider_resource_id)
                    report.reaped += 1
                    over_by -= 1
            finally:
                await session.close()
