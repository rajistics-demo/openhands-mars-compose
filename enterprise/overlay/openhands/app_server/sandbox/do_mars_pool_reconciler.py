"""The reconciliation loop for the opt-in DO MARS warm pool.

One tick (``run_once``) does, in order: recover stuck claims, reap
unhealthy/stale/error inventory, health-check hot slots (safe to dial -
they are already running and unclaimed), health-check paused slots
(control-plane status only - never dialed, see
do_mars_pool_provider.PoolProvider), reconcile in-flight BOOTING slots,
replenish/rebalance toward the configured hot and paused-reserve targets,
then - last, deliberately after every step above that can *initiate* one -
confirm in-flight PAUSING/RESUMING slots and pending DELETING deletions (a
provider read against every DELETING row - see ``_destroy``'s own
docstring for why that never finalizes synchronously). Running the confirm
steps last lets a transition initiated earlier in the very same tick get
confirmed in that same tick when the provider is fast enough, without
ever requiring it: each confirm step re-scans with no "this tick only"
filter, so a transition the provider is still settling on just gets
picked up again next tick, exactly as before.

Every step is scoped to one (backend, sandbox_spec_id, config_id) triple and
every state-changing action commits its own short transaction, so a crash
between ticks or between two of these steps never leaves more than one
command's worth of work ambiguous, and the next tick's snapshot-based
decisions are always conservative (re-check, never assume).

Every transition this module makes *out of* a state ``claim_one`` can also
select (HOT_READY, PAUSED_RESERVE) goes through
``do_mars_pool_store.set_state_if``'s compare-and-swap, never a plain
``set_state``: an unlocked snapshot read (``select_sweepable``) is never by
itself sufficient grounds to act on a row that a concurrent
tenant-facing claim could take in the meantime - see ``_destroy`` and the
docstrings of ``_health_check_hot``/``_demote_excess_hot``/
``_promote_one_paused`` for exactly which window each one closes.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

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
    set_state_if,
)

logger = logging.getLogger(__name__)

# The only two DO Harness Runtime session statuses that mean "gone", per
# do_mars_sandbox_service._status_for's MISSING mapping. Duplicated here
# (two literal strings, not that function or its full STARTING/ERROR
# enumeration) rather than imported, because do_mars_sandbox_service.py
# pulls in openhands.agent_server.utils - the one Enterprise-only import the
# do_mars_pool_* modules are deliberately kept free of, so a reconciler unit
# test can still run against a pure fake with no Enterprise source present.
# Any other status (including every real in-progress one, and one this
# constant doesn't yet know about) is treated as "still possibly booting,
# ask again next tick" by _reconcile_booting, never as a reason to destroy
# on its own - only confirmed absence or boot_timeout_seconds do that.
_TERMINAL_SESSION_STATUSES = ('SESSION_STATUS_DESTROYED', 'SESSION_STATUS_DELETED')


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
    # Every timestamp column this reconciler compares against (created_at,
    # state_changed_at, ...) is TIMESTAMP WITH TIME ZONE (see do_mars_002's
    # migration), which asyncpg/SQLAlchemy returns as timezone-aware
    # datetimes - never datetime.utcnow, which is naive. Confirmed on a
    # real deployment against real PostgreSQL: every one of this module's
    # `self.now() - timedelta(...)` deadline comparisons against a slot's
    # own aware timestamp raised TypeError ("can't compare offset-naive
    # and offset-aware datetimes"), caught by run_once's own top-level
    # try/except and reported as a tick failure every single tick - this
    # was never exercised by the test suite because its own
    # make_reconciler helper always supplies an explicit aware `now`.
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

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
            # Populated by every _destroy() call this tick (regardless of
            # which step initiated it) so _confirm_pending_deletions can
            # tell "just marked DELETING a moment ago, this same tick"
            # apart from "has genuinely been DELETING since a prior tick".
            # That distinction matters because self.now() can legitimately
            # be mocked ahead of the row's own wall-clock-stamped
            # state_changed_at (tests do this deliberately, e.g. to make a
            # BOOTING slot's created_at look past boot_timeout_seconds
            # without a real sleep) - without it, a same-tick DELETING
            # transition could look spuriously past
            # delete_confirm_timeout_seconds and get a redundant
            # delete_session re-issue plus a false "unconfirmed" error on
            # the very tick that created it.
            fresh_deletions: set[str] = set()
            try:
                await self._recover_stuck_claims(report, fresh_deletions)
                await self._reap(report, fresh_deletions)
                await self._health_check_hot(report)
                await self._health_check_paused(report)
                await self._reconcile_booting(report, fresh_deletions)
                if self.config.replenish_enabled:
                    await self._replenish_and_rebalance(report, fresh_deletions)
                # The three confirm/finalize steps run last, deliberately
                # after every step above that can *initiate* a PAUSING/
                # RESUMING/DELETING transition this same tick (reap,
                # reconcile_booting, replenish_and_rebalance's own
                # demote/promote/shed) - not before them. Each confirm step
                # also still re-scans for leftovers from a *prior* tick
                # (select_sweepable has no "just mine" filter), so this
                # ordering only ever adds a same-tick confirmation chance
                # on top of that, never removes the cross-tick retry path
                # BOOTING/PAUSING/RESUMING/DELETING all otherwise rely on
                # when the provider is slower than one tick to confirm.
                await self._reconcile_pausing(report)
                await self._reconcile_resuming(report)
                await self._confirm_pending_deletions(report, fresh_deletions)
            except Exception as exc:  # noqa: BLE001 - one tick's fault must not crash the loop
                logger.exception('do_mars_pool.tick_failed')
                report.errors.append(str(exc))
            return report
        finally:
            await _unlock(lock_session, key)
            await lock_session.close()

    async def _recover_stuck_claims(self, report: PoolTickReport, fresh_deletions: set[str]) -> None:
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
        assert self.config.sandbox_spec_id is not None
        scan_session = self.session_factory()
        try:
            candidate_ids = [
                slot.id
                for slot in await select_stale_claiming(
                    scan_session,
                    self.backend,
                    self.config.sandbox_spec_id,
                    self.config_id,
                    deadline,
                )
            ]
        finally:
            await scan_session.close()

        for slot_id in candidate_ids:
            session = self.session_factory()
            try:
                locked = await lock_stale_claiming_slot_for_recovery(
                    session,
                    backend=self.backend,
                    sandbox_spec_id=self.config.sandbox_spec_id,
                    config_id=self.config_id,
                    slot_id=slot_id,
                    older_than=deadline,
                )
                if locked is None:
                    # Either a promotion is concurrently holding this row's
                    # lock, or the row has already left CLAIMING (promoted
                    # or recovered) since the scan above. Either way,
                    # nothing to do this tick.
                    await session.rollback()
                    continue
                destroyed = await self._destroy(
                    session,
                    locked.id,
                    locked.provider_resource_id,
                    expected_states=(PoolSlotState.CLAIMING,),
                )
                if destroyed:
                    report.recovered_stuck_claims += 1
                    fresh_deletions.add(locked.id)
            finally:
                await session.close()

    async def _destroy(
        self,
        session: AsyncSession,
        slot_id: str,
        provider_resource_id: str | None,
        *,
        expected_states: tuple[PoolSlotState, ...],
    ) -> bool:
        """Durable deletion: make the slot non-claimable and persist that
        fact to PostgreSQL *before* any provider call, never after.

        ``expected_states`` is a compare-and-swap guard
        (``do_mars_pool_store.set_state_if``), not a formality: every
        caller except the already-FOR-UPDATE-locked CLAIMING-recovery path
        reads its candidates from an unlocked ``select_sweepable`` snapshot
        (HOT_READY/PAUSED_RESERVE/ERROR/BOOTING), and ``claim_one`` can take
        a HOT_READY or PAUSED_RESERVE row for a real tenant at any moment in
        between that read and this call. Returns ``False`` without ever
        calling the provider at all if that race was lost - the slot
        belongs to its claimant now, and must not be touched, let alone
        destroyed, by this tick.

        Once the DELETING transition is durable, this issues one best-effort
        provider delete call and then stops - it never itself confirms the
        resource is actually gone, and never drops the local row on the
        strength of the delete call's own return value. DoMarsClient.
        delete_session treats 200/202/204/404 all as a non-raising
        "success", which for DO's asynchronous delete means only "the
        request was accepted", not "the VM is gone" - treating that as
        confirmation is exactly the gap this method closes.
        ``_confirm_pending_deletions`` is what actually drops the row later,
        once a provider read - not a delete call's status code - confirms
        the resource is missing or destroyed.

        The one exception: a slot with no ``provider_resource_id`` at all
        (only ``_create_one``'s ambiguous-create-failure path ever leaves
        CREATING in that shape, and that path never reaches ``_destroy`` -
        see its own docstring) has nothing for any provider read to ever
        confirm, so there is nothing to wait for; the row is dropped
        immediately in that case.
        """

        applied = await set_state_if(
            session,
            slot_id,
            expected_states=expected_states,
            state=PoolSlotState.DELETING,
        )
        if not applied:
            return False
        if not provider_resource_id:
            await delete_slot(session, slot_id)
            return True
        try:
            await self.provider.delete_session(provider_resource_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                'do_mars_pool.destroy_provider_delete_failed',
                extra={'slot_id': slot_id, 'error': str(exc)},
            )
        return True

    async def _confirm_pending_deletions(
        self, report: PoolTickReport, fresh_deletions: set[str]
    ) -> None:
        """The second half of durable deletion: for every DELETING slot,
        ask the provider (never trust ``delete_session``'s own return) and
        only drop the local row once a read confirms the resource is
        missing or destroyed.

        Bounded, never unbounded: past ``delete_confirm_timeout_seconds``
        with still no confirmation, the delete call is re-issued
        (best-effort, harmless if already accepted) and the situation is
        reported loudly via ``report.errors`` - but the row is still never
        dropped without that confirmation. "Retain until an exact provider
        read confirms missing or destroyed" has no time limit in the one
        direction that matters: silently declaring a VM gone because
        waiting got inconvenient is exactly the kind of mistake this whole
        DELETING state exists to prevent.
        """

        assert self.config.sandbox_spec_id is not None
        session = self.session_factory()
        try:
            candidates = await select_sweepable(
                session,
                self.backend,
                self.config.sandbox_spec_id,
                self.config_id,
                (PoolSlotState.DELETING,),
                limit=20,
            )
        finally:
            await session.close()

        too_old = self.now() - timedelta(seconds=self.config.delete_confirm_timeout_seconds)
        for slot in candidates:
            if not slot.provider_resource_id:
                # _destroy never leaves a DELETING row with no provider
                # reference (it finalizes those immediately) - unexpected,
                # leave for an operator rather than guess either way.
                continue
            session = self.session_factory()
            try:
                confirmed_gone = False
                try:
                    remaining = await self.provider.get_session(slot.provider_resource_id)
                    confirmed_gone = (
                        remaining is None or remaining.status in _TERMINAL_SESSION_STATUSES
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        'do_mars_pool.delete_confirm_read_failed',
                        extra={'slot_id': slot.id, 'error': str(exc)},
                    )
                if confirmed_gone:
                    await delete_slot(session, slot.id)
                    # No counter bump here: unlike the create/BOOTING path
                    # (where report.created only ever fires once, at
                    # whichever single step - immediate or deferred -
                    # first confirms readiness), a destroy's outcome is
                    # already fully decided the moment _destroy's CAS
                    # lands on DELETING - there is no second possible
                    # outcome left to wait for, only how long dropping the
                    # row takes. Every _destroy caller already counts the
                    # decision once, right there (recovered_stuck_claims
                    # or reaped); counting again here would double-count
                    # every slot this step ever finalizes, same-tick or
                    # not.
                    pass
                elif slot.id in fresh_deletions:
                    # DELETING was set earlier in this very tick: too_old is
                    # computed from self.now(), which a caller is free to
                    # mock ahead of real time for an unrelated check
                    # elsewhere in the same tick (e.g. boot_timeout_seconds)
                    # - comparing it against this row's wall-clock
                    # state_changed_at could then look spuriously overdue
                    # despite being seconds old. Left as DELETING, retried
                    # next tick like any other not-yet-confirmed deletion;
                    # no redundant delete_session re-issue, no false
                    # "unconfirmed" error for a deletion this same tick only
                    # just started.
                    pass
                elif slot.state_changed_at < too_old:
                    try:
                        await self.provider.delete_session(slot.provider_resource_id)
                    except Exception:  # noqa: BLE001
                        pass
                    report.errors.append(
                        f'pool slot {slot.id} deletion unconfirmed past '
                        'delete_confirm_timeout_seconds; provider resource '
                        f'{slot.provider_resource_id!r} needs operator reconciliation'
                    )
                # else: still within the window and not yet confirmed -
                # left exactly as DELETING, retried next tick.
            finally:
                await session.close()

    async def _reap(self, report: PoolTickReport, fresh_deletions: set[str]) -> None:
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
                    # expected_states pins the compare-and-swap to the exact
                    # state this slot was just read as - HOT_READY and
                    # PAUSED_RESERVE are both claimable, so a concurrent
                    # claim_one may have already taken this row; ERROR never
                    # is, so its own transition always succeeds unless some
                    # other reconciler step reached it first this tick.
                    if await self._destroy(
                        session,
                        slot.id,
                        slot.provider_resource_id,
                        expected_states=(slot.state,),
                    ):
                        report.reaped += 1
                        fresh_deletions.add(slot.id)
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

    async def _replenish_and_rebalance(
        self, report: PoolTickReport, fresh_deletions: set[str]
    ) -> None:
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

        # Hot is already at or above target (the branch above did not fire),
        # but paused reserve is still short and there is headroom: create
        # one more VM. It always lands HOT_READY first - a create can never
        # land directly in PAUSED_RESERVE, since pausing a slot that was
        # never dialed/confirmed would violate PoolProvider's "never probe a
        # paused VM" contract the other direction (nothing has even
        # confirmed it boots yet) - and the branch above this one then
        # demotes the resulting hot-over-target slot into paused_reserve on
        # a later tick. Without this branch, a cold/empty pool
        # (hot=0, paused=0) with min_paused_reserve>0 fills hot up to
        # min_hot and then does nothing forever: paused_reserve is never
        # funded, because every other path into it requires hot to
        # *already* be over target first.
        if counts.paused_reserve < self.config.min_paused_reserve and (
            counts.total_inventory < self.config.max_total
        ):
            await self._create_one(report)
            return

        if counts.total_inventory > self.config.max_total:
            await self._shed_excess(report, counts, fresh_deletions)

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
                # tick, by _reconcile_booting below - not by age-based
                # max_slot_age_seconds reaping, which is a much longer
                # (hours, not boot_timeout_seconds) backstop meant for
                # already-hot/paused inventory, not an in-progress boot.
                report.boot_failed += 1
        finally:
            await session.close()

    async def _reconcile_booting(
        self, report: PoolTickReport, fresh_deletions: set[str]
    ) -> None:
        """Give a BOOTING slot more than the one dormant-probe attempt
        ``_create_one`` makes before handing control back to the tick loop.

        Without this step, a slot not yet dormant-ready on its *first*
        check was never looked at again: ``select_sweepable``'s normal
        sweep states (HOT_READY/PAUSED_RESERVE/ERROR) never include
        BOOTING, so the row just sat there forever, holding one
        in_flight capacity slot, never claimable and never cleaned up.
        Confirmed on a real DO MARS deployment: the dormant probe issued
        immediately after ``create_session`` returns almost always loses
        the race against the guest's own boot time, so this is not a
        rare edge case.

        One provider call per candidate per tick, no blocking sleep - a
        boot that needs longer just gets looked at again next tick:
          - dormant probe succeeds -> promote to HOT_READY, fresh health
            (report.created, same counter _create_one's own happy path
            uses - both are "a boot finished" events).
          - the session is confirmed gone (``get_session`` returns
            ``None``) or reports one of the two known terminal statuses
            -> nothing left to wait for; queued for durable deletion now,
            not held open until boot_timeout_seconds (report.reaped,
            same counter every other destroy path uses).
          - anything else - every in-progress status, an unreadable
            status, or a ``get_session`` failure - while still within
            ``boot_timeout_seconds`` of ``created_at`` -> left exactly as
            BOOTING, retried next tick (report.boot_failed).
          - past boot_timeout_seconds with no confirmed-ready probe ->
            queued for durable deletion, same as confirmed-gone.
        """

        assert self.config.sandbox_spec_id is not None
        session = self.session_factory()
        try:
            candidates = await select_sweepable(
                session,
                self.backend,
                self.config.sandbox_spec_id,
                self.config_id,
                (PoolSlotState.BOOTING,),
                limit=20,
            )
        finally:
            await session.close()

        too_old = self.now() - timedelta(seconds=self.config.boot_timeout_seconds)
        for slot in candidates:
            if not slot.provider_resource_id:
                # Cannot happen via _create_one (BOOTING is only ever
                # entered with provider_resource_id already set), but a
                # row with no provider reference has nothing this step
                # can probe or destroy - leave it for an operator.
                continue
            session = self.session_factory()
            try:
                try:
                    ready = await self.provider.probe_dormant(slot.provider_resource_id)
                except Exception:  # noqa: BLE001
                    ready = False
                if ready:
                    await set_state(
                        session, slot.id, PoolSlotState.HOT_READY, last_health_ok=True
                    )
                    report.created += 1
                    continue

                terminal = False
                try:
                    remaining = await self.provider.get_session(slot.provider_resource_id)
                    terminal = (
                        remaining is None or remaining.status in _TERMINAL_SESSION_STATUSES
                    )
                except Exception:  # noqa: BLE001
                    # An unreadable status is not itself confirmation of
                    # anything - fall through to the same transient/
                    # timeout handling a genuinely-still-booting slot gets.
                    pass

                if terminal or slot.created_at < too_old:
                    if await self._destroy(
                        session,
                        slot.id,
                        slot.provider_resource_id,
                        expected_states=(PoolSlotState.BOOTING,),
                    ):
                        report.reaped += 1
                        fresh_deletions.add(slot.id)
                else:
                    report.boot_failed += 1
            finally:
                await session.close()

    async def _promote_one_paused(self, report: PoolTickReport) -> None:
        """Issue one resume_session call and leave the slot RESUMING -
        confirmation is a separate step (_reconcile_resuming), because a
        real resume can easily outlast one tick, same reasoning as
        _reconcile_booting for a create.
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
                limit=1,
            )
            if not candidates:
                return
            slot = candidates[0]
            # Compare-and-swap, not a plain set_state: claim_one's default
            # states include PAUSED_RESERVE, so a tenant claim can take this
            # exact row between the unlocked select above and this write.
            # Losing the race means the row is no longer this method's to
            # touch at all - not even to issue resume_session against,
            # which would wake a VM a claimant may already be using.
            applied = await set_state_if(
                session,
                slot.id,
                expected_states=(PoolSlotState.PAUSED_RESERVE,),
                state=PoolSlotState.RESUMING,
            )
        finally:
            await session.close()
        if not applied:
            return

        session = self.session_factory()
        try:
            try:
                await self.provider.resume_session(slot.provider_resource_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    'do_mars_pool.resume_issue_failed',
                    extra={'slot_id': slot.id, 'error': str(exc)},
                )
        finally:
            await session.close()

    async def _demote_excess_hot(self, report: PoolTickReport, counts) -> None:
        """Issue one pause_session call per slot and leave it PAUSING -
        confirmation is a separate step (_reconcile_pausing); see
        _promote_one_paused's docstring for why.
        """

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
                # Compare-and-swap: claim_one's default states include
                # HOT_READY, so a concurrent claim can take this row between
                # the unlocked select above and this write - losing the
                # race means do not pause a VM a claimant may already own.
                applied = await set_state_if(
                    session,
                    slot.id,
                    expected_states=(PoolSlotState.HOT_READY,),
                    state=PoolSlotState.PAUSING,
                )
            finally:
                await session.close()
            if not applied:
                continue
            session = self.session_factory()
            try:
                try:
                    await self.provider.pause_session(slot.provider_resource_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        'do_mars_pool.pause_issue_failed',
                        extra={'slot_id': slot.id, 'error': str(exc)},
                    )
            finally:
                await session.close()

    async def _reconcile_pausing(self, report: PoolTickReport) -> None:
        """Confirm (never dial - is_still_paused is control-plane-status
        only) each PAUSING slot across as many ticks as it takes, instead
        of the one immediate check _demote_excess_hot used to make right
        after issuing pause_session. DO's pause settles asynchronously;
        one unconfirmed check right away is not evidence of failure, only
        of "not yet" - mirrors _reconcile_booting's shape exactly.
        """

        assert self.config.sandbox_spec_id is not None
        session = self.session_factory()
        try:
            candidates = await select_sweepable(
                session,
                self.backend,
                self.config.sandbox_spec_id,
                self.config_id,
                (PoolSlotState.PAUSING,),
                limit=20,
            )
        finally:
            await session.close()

        too_old = self.now() - timedelta(
            seconds=self.config.control_op_confirm_timeout_seconds
        )
        for slot in candidates:
            if not slot.provider_resource_id:
                continue
            session = self.session_factory()
            try:
                try:
                    ok = await self.provider.is_still_paused(slot.provider_resource_id)
                except Exception:  # noqa: BLE001
                    ok = False
                if ok:
                    await set_state(
                        session, slot.id, PoolSlotState.PAUSED_RESERVE, last_health_ok=True
                    )
                    report.paused_for_demotion += 1
                elif slot.state_changed_at < too_old:
                    await set_state(
                        session,
                        slot.id,
                        PoolSlotState.ERROR,
                        error_detail='pause did not confirm within control_op_confirm_timeout_seconds',
                    )
                # else: still within the window, not yet confirmed - left
                # exactly as PAUSING, retried next tick.
            finally:
                await session.close()

    async def _reconcile_resuming(self, report: PoolTickReport) -> None:
        """Confirm each RESUMING slot across as many ticks as it takes,
        mirroring _reconcile_pausing. probe_alive (a dial) is correct here,
        not a contract violation of "never dial a paused slot": by now the
        slot has been explicitly resumed, exactly the same resume-then-dial
        order _promote_one_paused always used.
        """

        assert self.config.sandbox_spec_id is not None
        session = self.session_factory()
        try:
            candidates = await select_sweepable(
                session,
                self.backend,
                self.config.sandbox_spec_id,
                self.config_id,
                (PoolSlotState.RESUMING,),
                limit=20,
            )
        finally:
            await session.close()

        too_old = self.now() - timedelta(
            seconds=self.config.control_op_confirm_timeout_seconds
        )
        for slot in candidates:
            if not slot.provider_resource_id:
                continue
            session = self.session_factory()
            try:
                try:
                    ok = await self.provider.probe_alive(slot.provider_resource_id)
                except Exception:  # noqa: BLE001
                    ok = False
                if ok:
                    await set_state(
                        session, slot.id, PoolSlotState.HOT_READY, last_health_ok=True
                    )
                    report.resumed_for_promotion += 1
                elif slot.state_changed_at < too_old:
                    await set_state(
                        session,
                        slot.id,
                        PoolSlotState.ERROR,
                        error_detail='resume did not confirm within control_op_confirm_timeout_seconds',
                    )
                # else: still within the window, not yet confirmed - left
                # exactly as RESUMING, retried next tick.
            finally:
                await session.close()

    async def _shed_excess(
        self, report: PoolTickReport, counts, fresh_deletions: set[str]
    ) -> None:
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
                    if await self._destroy(
                        session,
                        slot.id,
                        slot.provider_resource_id,
                        expected_states=states,
                    ):
                        report.reaped += 1
                        over_by -= 1
                        fresh_deletions.add(slot.id)
                    # else: claim_one won this row first - it no longer
                    # counts toward the excess being shed this tick, but it
                    # is also no longer this method's to touch at all.
            finally:
                await session.close()
