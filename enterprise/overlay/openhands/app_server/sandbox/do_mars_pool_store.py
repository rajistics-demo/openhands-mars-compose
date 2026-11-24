"""Storage for the opt-in DO MARS warm-pool inventory table.

Uses a plain SQLAlchemy Core Table bound to its own MetaData, not the
Enterprise declarative Base used by StoredSandbox. do_mars_pool_slot is a new
table with no owner columns and no relationship to v1_remote_sandbox; a Core
Table needs only a MetaData instance, which this module creates for itself,
so adding this inventory needs no access to, or assumption about, the
Enterprise ORM base class. The table is created by the do_mars_002 migration,
not by any create_all() call here.

Every query below is scoped by (backend, sandbox_spec_id, config_id): pool
inventory for one operator-configured MARS environment config never leaks
into another spec's counts, claims or sweeps.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    case,
    delete,
    func,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.app_server.sandbox.do_mars_pool_models import (
    PoolCounts,
    PoolSlot,
    PoolSlotState,
)


class _Unset:
    __slots__ = ()


_unset = _Unset()

_metadata = MetaData()

pool_slot_table = Table(
    'do_mars_pool_slot',
    _metadata,
    Column('id', String, primary_key=True),
    Column('backend', String, nullable=False),
    Column('sandbox_spec_id', String, nullable=False),
    Column('config_id', String, nullable=False),
    Column('state', String, nullable=False),
    Column('provider_resource_id', String, nullable=True),
    Column('provider_create_intent_at', DateTime(timezone=True), nullable=True),
    Column('created_at', DateTime(timezone=True), nullable=False),
    Column('state_changed_at', DateTime(timezone=True), nullable=False),
    Column('last_health_check_at', DateTime(timezone=True), nullable=True),
    Column('last_health_ok', Boolean, nullable=True),
    Column('claim_owner_sandbox_id', String, nullable=True),
    Column('claimed_by_user_id', String, nullable=True),
    Column('claimed_org_id', String, nullable=True),
    Column('error_detail', String, nullable=True),
)


def _row_to_slot(row) -> PoolSlot:
    return PoolSlot(
        id=row.id,
        backend=row.backend,
        sandbox_spec_id=row.sandbox_spec_id,
        config_id=row.config_id,
        state=PoolSlotState(row.state),
        provider_resource_id=row.provider_resource_id,
        provider_create_intent_at=row.provider_create_intent_at,
        created_at=row.created_at,
        state_changed_at=row.state_changed_at,
        last_health_check_at=row.last_health_check_at,
        last_health_ok=row.last_health_ok,
        claim_owner_sandbox_id=row.claim_owner_sandbox_id,
        claimed_by_user_id=row.claimed_by_user_id,
        claimed_org_id=row.claimed_org_id,
        error_detail=row.error_detail,
    )


async def insert_creating_slot(
    db_session: AsyncSession,
    *,
    slot_id: str,
    backend: str,
    sandbox_spec_id: str,
    config_id: str,
    intent_at: datetime,
) -> None:
    """Row-first intent, mirroring the non-pool create-intent pattern (D4).

    Commits immediately so a crash before the provider call leaves a
    discoverable CREATING row rather than an orphaned provider session with
    no local record at all.
    """

    now = intent_at
    await db_session.execute(
        pool_slot_table.insert().values(
            id=slot_id,
            backend=backend,
            sandbox_spec_id=sandbox_spec_id,
            config_id=config_id,
            state=PoolSlotState.CREATING,
            provider_resource_id=None,
            provider_create_intent_at=intent_at,
            created_at=now,
            state_changed_at=now,
        )
    )
    await db_session.commit()


async def set_state(
    db_session: AsyncSession,
    slot_id: str,
    state: PoolSlotState,
    *,
    provider_resource_id: str | None | object = _unset,
    last_health_ok: bool | None | object = _unset,
    error_detail: str | None | object = _unset,
    commit: bool = True,
) -> None:
    """Advance one slot's state. Unset keyword args leave that column alone."""

    values: dict = {'state': state, 'state_changed_at': func.now()}
    if provider_resource_id is not _unset:
        values['provider_resource_id'] = provider_resource_id
    if last_health_ok is not _unset:
        values['last_health_ok'] = last_health_ok
        values['last_health_check_at'] = func.now()
    if error_detail is not _unset:
        values['error_detail'] = error_detail
    await db_session.execute(
        update(pool_slot_table).where(pool_slot_table.c.id == slot_id).values(**values)
    )
    if commit:
        await db_session.commit()


async def set_state_if(
    db_session: AsyncSession,
    slot_id: str,
    *,
    expected_states: tuple[PoolSlotState, ...],
    state: PoolSlotState,
    provider_resource_id: str | None | object = _unset,
    last_health_ok: bool | None | object = _unset,
    error_detail: str | None | object = _unset,
    commit: bool = True,
) -> bool:
    """Compare-and-swap: like ``set_state``, but only applies if the row is
    currently in one of ``expected_states``. Returns ``True`` if the
    transition was applied, ``False`` if it was not - most commonly because
    ``claim_one`` already won a race and moved the row to CLAIMING first.

    No ``FOR UPDATE``/``SKIP LOCKED`` needed: a plain ``UPDATE ... WHERE
    id = ? AND state IN (?)`` is itself safe against a concurrent
    ``claim_one`` on the same row, because Postgres serializes any two
    UPDATEs that target the same row - if claim_one's own update is
    in-flight (uncommitted) when this one is issued, this one simply blocks
    until claim_one commits (or rolls back), then its WHERE clause is
    re-evaluated against the now-current (committed) state. If claim_one
    committed CLAIMING, the WHERE no longer matches and this returns
    ``False``; if claim_one touched a different row (``SKIP LOCKED`` means
    it only ever locks rows it does not skip), there was never any
    contention at all.

    A caller that gets ``False`` back must stop, not retry the same
    transition or fall back to an unconditional ``set_state``: the row now
    belongs to whatever concurrent writer won, and is no longer this
    caller's to act on.
    """

    values: dict = {'state': state, 'state_changed_at': func.now()}
    if provider_resource_id is not _unset:
        values['provider_resource_id'] = provider_resource_id
    if last_health_ok is not _unset:
        values['last_health_ok'] = last_health_ok
        values['last_health_check_at'] = func.now()
    if error_detail is not _unset:
        values['error_detail'] = error_detail
    result = await db_session.execute(
        update(pool_slot_table)
        .where(
            pool_slot_table.c.id == slot_id,
            pool_slot_table.c.state.in_([s.value for s in expected_states]),
        )
        .values(**values)
    )
    applied = result.rowcount == 1
    if commit:
        await db_session.commit()
    return applied


async def delete_slot(db_session: AsyncSession, slot_id: str, *, commit: bool = True) -> None:
    await db_session.execute(delete(pool_slot_table).where(pool_slot_table.c.id == slot_id))
    if commit:
        await db_session.commit()


async def get_slot(db_session: AsyncSession, slot_id: str) -> PoolSlot | None:
    row = (
        await db_session.execute(select(pool_slot_table).where(pool_slot_table.c.id == slot_id))
    ).first()
    return _row_to_slot(row) if row else None


async def count_by_state(
    db_session: AsyncSession, backend: str, sandbox_spec_id: str, config_id: str
) -> PoolCounts:
    """One snapshot of inventory for a single spec/config pair.

    Read with a plain (lock-free) query: the reconciler uses this only to
    decide whether to attempt a create or a pause, and every state-changing
    action it then takes is itself guarded by its own row-level claim or
    update, so a snapshot that is a moment stale only ever causes a
    conservative retry on the next tick, never an unsafe double-action.
    """

    scope = (
        pool_slot_table.c.backend == backend,
        pool_slot_table.c.sandbox_spec_id == sandbox_spec_id,
        pool_slot_table.c.config_id == config_id,
    )
    in_flight_states = (
        PoolSlotState.CREATING,
        PoolSlotState.BOOTING,
        PoolSlotState.PAUSING,
        PoolSlotState.RESUMING,
        PoolSlotState.CLAIMING,
    )
    rows = (
        await db_session.execute(
            select(pool_slot_table.c.state, func.count())
            .where(*scope)
            .group_by(pool_slot_table.c.state)
        )
    ).all()
    tally = {state: count for state, count in rows}
    return PoolCounts(
        hot_ready=tally.get(PoolSlotState.HOT_READY.value, 0),
        paused_reserve=tally.get(PoolSlotState.PAUSED_RESERVE.value, 0),
        in_flight=sum(tally.get(state.value, 0) for state in in_flight_states),
        error=tally.get(PoolSlotState.ERROR.value, 0),
        deleting=tally.get(PoolSlotState.DELETING.value, 0),
    )


async def select_sweepable(
    db_session: AsyncSession,
    backend: str,
    sandbox_spec_id: str,
    config_id: str,
    states: tuple[PoolSlotState, ...],
    *,
    limit: int,
) -> list[PoolSlot]:
    """Slots in one of ``states``, oldest first, for a size/health/age/boot
    sweep. A plain unlocked read: safe for every state a caller passes here
    today because none of them is both externally claimable and reached by
    this function without a guard elsewhere - PoolSlotState.unclaimed_idle
    states (HOT_READY/PAUSED_RESERVE/ERROR) are claimable, but every
    destructive action this module's callers take on a row read here either
    first re-locks/re-confirms it (see do_mars_pool_reconciler._destroy and
    its callers) or targets BOOTING, which claim_one never selects. Never
    pass CLAIMING or CLAIMED: both are already claimed/being claimed, and
    this function applies no lock or compare-and-swap of its own.
    """

    rows = (
        await db_session.execute(
            select(pool_slot_table)
            .where(
                pool_slot_table.c.backend == backend,
                pool_slot_table.c.sandbox_spec_id == sandbox_spec_id,
                pool_slot_table.c.config_id == config_id,
                pool_slot_table.c.state.in_([s.value for s in states]),
            )
            .order_by(pool_slot_table.c.created_at)
            .limit(limit)
        )
    ).all()
    return [_row_to_slot(row) for row in rows]


async def claim_one(
    db_session: AsyncSession,
    *,
    backend: str,
    sandbox_spec_id: str,
    config_id: str,
    claim_owner_sandbox_id: str,
    claimed_by_user_id: str | None,
    claimed_org_id: str | None,
    min_health_check_after: datetime,
    states: tuple[PoolSlotState, ...] = (
        PoolSlotState.HOT_READY,
        PoolSlotState.PAUSED_RESERVE,
    ),
) -> PoolSlot | None:
    """Atomically take exactly one eligible slot, or none.

    ``SELECT ... FOR UPDATE SKIP LOCKED`` lets any number of app/worker
    processes race this call concurrently: each sees only rows no other
    transaction currently holds, so two callers can never select the same
    slot id, and a caller that finds nothing never blocks waiting on one
    that is mid-claim. The row is updated to CLAIMING and committed in the
    same transaction that took the lock, before any provider call is made,
    so the claim is durable even if this process crashes immediately after.
    A slot without a fresh health check (older than
    ``min_health_check_after``, or never checked) is never selected: an
    unknown-health slot is the reconciler's job to probe or reap, not a
    claim's job to gamble on.

    ``states`` defaults to both HOT_READY and PAUSED_RESERVE (preferring hot
    - see the ordering below) so existing/future callers that can resume a
    paused VM inline keep today's behavior unchanged. A caller that cannot
    resume inline (e.g. ``DoMarsSandboxService.start_sandbox``, which must
    not add a "wait for resume" path to an already-dense create flow) should
    pass ``states=(PoolSlotState.HOT_READY,)`` explicitly, never filter a
    returned PAUSED_RESERVE slot after the fact: by then it is already
    CLAIMING and committed, so silently ignoring it would strand it until
    the reconciler's stuck-claim timeout.
    """

    candidates = select(pool_slot_table.c.id).where(
        pool_slot_table.c.backend == backend,
        pool_slot_table.c.sandbox_spec_id == sandbox_spec_id,
        pool_slot_table.c.config_id == config_id,
        pool_slot_table.c.state.in_([s.value for s in states]),
        pool_slot_table.c.last_health_ok.is_(True),
        pool_slot_table.c.last_health_check_at.is_not(None),
        pool_slot_table.c.last_health_check_at >= min_health_check_after,
    ).order_by(
        # A hot slot serves a tenant immediately; a paused one needs a
        # resume first, so prefer hot whenever both are eligible.
        case((pool_slot_table.c.state == PoolSlotState.HOT_READY.value, 0), else_=1),
        pool_slot_table.c.created_at,
    ).limit(1).with_for_update(skip_locked=True)

    slot_id = (await db_session.execute(candidates)).scalars().first()
    if slot_id is None:
        await db_session.rollback()
        return None

    await db_session.execute(
        update(pool_slot_table)
        .where(pool_slot_table.c.id == slot_id)
        .values(
            state=PoolSlotState.CLAIMING.value,
            state_changed_at=func.now(),
            claim_owner_sandbox_id=claim_owner_sandbox_id,
            claimed_by_user_id=claimed_by_user_id,
            claimed_org_id=claimed_org_id,
        )
    )
    claimed = await get_slot(db_session, slot_id)
    await db_session.commit()
    return claimed


class ClaimPromotionError(RuntimeError):
    """Raised when a CLAIMING slot can no longer be promoted by this caller.

    Always means the caller must not create (or must roll back) the
    application-level sandbox row: nothing about the slot has been mutated
    when this is raised.
    """


async def promote_claim_to_sandbox(
    db_session: AsyncSession,
    *,
    slot_id: str,
    claim_owner_sandbox_id: str,
    create_sandbox_row: Callable[[AsyncSession], Awaitable[None]],
) -> None:
    """Atomically hand a CLAIMING slot's VM over to a new application sandbox.

    This is the one seam where pool inventory and the Enterprise sandbox
    table ever touch, and it exists to close exactly one crash window: a
    stale-CLAIMING row must be destroyable by the reconciler *only* when no
    application sandbox row was ever durably created for it. That is true
    if and only if "insert the application sandbox row" and "remove this
    slot from pool inventory" commit together, in the same transaction, on
    the same connection - so ``create_sandbox_row`` is called with this
    exact ``db_session`` (it must not open or commit its own transaction),
    and this function performs exactly one commit, after both writes. The
    pool slot row is deleted, not transitioned to CLAIMED, matching
    PoolSlotState's documented contract: once a claim succeeds, the row is
    "replaced by a v1_remote_sandbox row," not kept around as a second,
    parallel record of the same VM that a cleanup pass could later
    misinterpret as still being pool-owned.

    Preconditions, enforced here, not assumed from the caller's own record
    of a successful ``claim_one``: the slot must still exist, still be
    CLAIMING, and still be owned by ``claim_owner_sandbox_id``. ``FOR
    UPDATE`` re-locks the row so a concurrent reconciler tick computing
    staleness cannot interleave between this check and the commit below -
    it either already took the row (this call then fails closed) or it
    blocks until this transaction commits or rolls back, then re-reads the
    now-CLAIMED (or now-deleted) row and correctly does nothing.

    On any failure, the transaction is rolled back before raising: neither
    the sandbox row nor the CLAIMED transition is left half-applied, so the
    caller's only safe reaction to an exception here is "do not hand this
    VM to the tenant," never "retry the DB write alone."

    Requires this ``AsyncSession`` to be bound to a single direct (or
    session-pinned) connection, not one borrowed per-statement from a
    transaction-pooling proxy (e.g. PgBouncer in ``transaction`` mode):
    ``FOR UPDATE`` and the eventual commit must observe the same backend
    session. The advisory lock used by the reconciler's tick (see
    do_mars_pool_reconciler._try_tick_lock) has the same requirement, since
    ``pg_advisory_lock`` is tied to the backend session, not the
    transaction. A bring-your-own managed PostgreSQL used for this inventory
    must terminate in a session-mode connection for both code paths.
    """

    try:
        row = (
            await db_session.execute(
                select(pool_slot_table)
                .where(pool_slot_table.c.id == slot_id)
                .with_for_update()
            )
        ).first()
        if row is None:
            raise ClaimPromotionError(f'pool slot {slot_id} no longer exists')
        if row.state != PoolSlotState.CLAIMING.value:
            raise ClaimPromotionError(
                f'pool slot {slot_id} is {row.state!r}, not CLAIMING'
            )
        if row.claim_owner_sandbox_id != claim_owner_sandbox_id:
            raise ClaimPromotionError(
                f'pool slot {slot_id} is owned by a different claimant'
            )

        await create_sandbox_row(db_session)

        await db_session.execute(
            delete(pool_slot_table).where(pool_slot_table.c.id == slot_id)
        )
        await db_session.commit()
    except Exception:
        await db_session.rollback()
        raise


async def lock_stale_claiming_slot_for_recovery(
    db_session: AsyncSession,
    *,
    backend: str,
    sandbox_spec_id: str,
    config_id: str,
    slot_id: str,
    older_than: datetime,
) -> PoolSlot | None:
    """Re-lock and re-confirm one CLAIMING candidate immediately before a
    caller destroys it. ``select_stale_claiming`` is a cheap unlocked scan
    used only to find candidate ids; it is never itself sufficient grounds
    to destroy a VM, because a promotion can start and finish between that
    scan and any later action.

    ``sandbox_spec_id``/``config_id`` pin this to the exact provider-owned
    config identity this reconciler instance is scoped to - the same
    (backend, sandbox_spec_id, config_id) triple every other query in this
    module filters on (see the module docstring). Without them, a
    reconciler running one spec/config could destroy a stale CLAIMING row
    that in fact belongs to a *different* spec/config sharing the same
    backend, simply because both happened to use the same ``slot_id``
    namespace and pass the deadline check at the same moment.

    ``FOR UPDATE SKIP LOCKED`` makes this safe without blocking: if
    ``promote_claim_to_sandbox`` is concurrently holding this exact row's
    lock, this returns ``None`` immediately (the caller must treat that as
    "do nothing this tick, re-check next tick"), never waits for the lock
    and never destroys based on a stale read. By the time any caller sees a
    non-``None`` result here, it is holding the same row lock
    ``promote_claim_to_sandbox`` uses, on a row it has just confirmed is
    still CLAIMING and still past the deadline - so no promotion can
    possibly be concurrently in flight for it, and destroying it is
    provably, not probably, safe.

    Must be called with ``db_session`` left open (no commit/rollback)
    afterward, until the caller has finished acting on the lock - typically
    one immediate provider delete call followed by the row delete and a
    single commit, exactly as ``promote_claim_to_sandbox`` holds its lock
    across its own single commit.
    """

    row = (
        await db_session.execute(
            select(pool_slot_table)
            .where(
                pool_slot_table.c.id == slot_id,
                pool_slot_table.c.backend == backend,
                pool_slot_table.c.sandbox_spec_id == sandbox_spec_id,
                pool_slot_table.c.config_id == config_id,
                pool_slot_table.c.state == PoolSlotState.CLAIMING.value,
                pool_slot_table.c.state_changed_at < older_than,
            )
            .with_for_update(skip_locked=True)
        )
    ).first()
    return _row_to_slot(row) if row is not None else None


async def select_stale_claiming(
    db_session: AsyncSession,
    backend: str,
    sandbox_spec_id: str,
    config_id: str,
    older_than: datetime,
) -> list[PoolSlot]:
    """CLAIMING rows whose promotion never finished within the deadline,
    scoped to one exact (backend, sandbox_spec_id, config_id) - the same
    provider-owned config identity every other query in this module is
    scoped to (see the module docstring). A reconciler instance must never
    even consider, let alone destroy, a stale CLAIMING row that belongs to
    a different spec/config sharing the same backend.

    This is an unlocked scan for *candidates* only - it never itself
    justifies destroying anything, since a promotion can start and finish
    between this read and any later action. A caller must re-confirm (and
    lock) each candidate with ``lock_stale_claiming_slot_for_recovery``
    immediately before destroying it; see
    do_mars_pool_reconciler.DoMarsPoolReconciler._recover_stuck_claims.
    """

    rows = (
        await db_session.execute(
            select(pool_slot_table).where(
                pool_slot_table.c.backend == backend,
                pool_slot_table.c.sandbox_spec_id == sandbox_spec_id,
                pool_slot_table.c.config_id == config_id,
                pool_slot_table.c.state == PoolSlotState.CLAIMING.value,
                pool_slot_table.c.state_changed_at < older_than,
            )
        )
    ).all()
    return [_row_to_slot(row) for row in rows]
