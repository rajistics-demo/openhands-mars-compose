"""Data shapes for the opt-in DO MARS warm-pool inventory.

Kept free of SQLAlchemy and of the Enterprise ``StoredSandbox``/``Base``
import surface so it can be imported and unit-tested without either. See
``do_mars_pool_store.py`` for the table definition and queries, and
``do_mars_pool_reconciler.py`` for the state machine that drives transitions.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class PoolSlotState(StrEnum):
    """Lifecycle of one pre-provisioned, not-yet-owned DO MARS session.

    CREATING -> BOOTING -> HOT_READY is a brand-new slot becoming available.
    HOT_READY -> PAUSING -> PAUSED_RESERVE is cost-driven demotion of excess
    hot inventory the operator still wants to keep as paused reserve.
    PAUSED_RESERVE -> RESUMING -> HOT_READY is promotion back to hot, done
    only by an explicit provider resume call, never a dial/probe.
    (HOT_READY | PAUSED_RESERVE) -> CLAIMING -> CLAIMED is a claim: CLAIMING
    is the only state with a non-null claim owner, and is the only state a
    slot ever leaves by being deleted (on success, replaced by a
    ``v1_remote_sandbox`` row; on failure, replaced by nothing because the
    underlying VM is destroyed). ERROR is a dead end reached by any state on
    an unrecoverable fault or staleness; ERROR rows are only ever deleted,
    never re-armed, because the VM behind them was touched or is unknown-good
    and must not be handed to any tenant.
    """

    CREATING = 'creating'
    BOOTING = 'booting'
    HOT_READY = 'hot_ready'
    PAUSING = 'pausing'
    PAUSED_RESERVE = 'paused_reserve'
    RESUMING = 'resuming'
    CLAIMING = 'claiming'
    # Reserved for a future tombstone/audit record. The current promotion
    # path (do_mars_pool_store.promote_claim_to_sandbox) never sets this: on
    # success it deletes the row in the same transaction that inserts the
    # v1_remote_sandbox row, rather than leaving a second, parallel record
    # of an already-owned VM.
    CLAIMED = 'claimed'
    ERROR = 'error'
    DELETING = 'deleting'

    @property
    def unclaimed_idle(self) -> bool:
        """States a size/age/health sweep may read, demote or reap freely.

        Deliberately excludes CREATING/BOOTING/RESUMING/PAUSING (an in-flight
        provider operation owns the row) and CLAIMING/CLAIMED (a tenant claim
        owns the row). A sweep that is not itself the state machine step for
        one of those never touches it.
        """

        return self in (
            PoolSlotState.HOT_READY,
            PoolSlotState.PAUSED_RESERVE,
            PoolSlotState.ERROR,
        )


@dataclass
class PoolSlot:
    id: str
    backend: str
    sandbox_spec_id: str
    config_id: str
    state: PoolSlotState
    provider_resource_id: str | None
    provider_create_intent_at: datetime | None
    created_at: datetime
    state_changed_at: datetime
    last_health_check_at: datetime | None
    last_health_ok: bool | None
    claim_owner_sandbox_id: str | None
    claimed_by_user_id: str | None
    claimed_org_id: str | None
    error_detail: str | None


@dataclass(frozen=True)
class PoolCounts:
    """A snapshot used to decide one reconciliation action; never cached."""

    hot_ready: int
    paused_reserve: int
    in_flight: int  # creating + booting + pausing + resuming + claiming
    error: int
    # A DELETING row still holds a real, not-yet-confirmed-gone provider VM
    # (see do_mars_pool_store.begin_delete / do_mars_pool_reconciler._destroy):
    # unlike ERROR, it is not "awaiting deletion" in the cheap local-row-only
    # sense, it IS mid-deletion, and the provider resource behind it may
    # still exist and still be billing/occupying capacity until a provider
    # read confirms otherwise. Counted in total_inventory for exactly that
    # reason - a replenish step must not create new capacity on top of
    # capacity that has not yet been confirmed freed.
    deleting: int = 0

    @property
    def total_inventory(self) -> int:
        """Hot + paused + in-flight + deleting: everything that counts
        against the cap.

        ERROR rows are excluded: they are awaiting deletion, hold no live
        claimable capacity, and must not block replenishment of real
        capacity while the reaper catches up. DELETING rows are the
        opposite case and are included - see the field's own docstring.
        """

        return self.hot_ready + self.paused_reserve + self.in_flight + self.deleting
