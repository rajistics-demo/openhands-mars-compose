"""Operator settings for the opt-in DO MARS warm pool.

Plain dataclass with an explicit ``from_env`` constructor: the pool is not a
``SandboxServiceInjector`` (it is not a selectable backend, there is still
exactly one do-mars backend), so it does not need the discriminated-union
injector machinery do_mars_sandbox_service.py uses. Disabled by default:
every field defaults to the "no pool" state, and DoMarsSandboxService only
imports/uses this module at all when ``DO_MARS_POOL_ENABLED=true``.

Bring-your-own managed PostgreSQL requirement (not expressible as a single
field here, so documented at the one place every operator reading this file
will see it): the pool's safety guarantees - ``do_mars_pool_store.claim_one``
and ``lock_stale_claiming_slot_for_recovery`` (``SELECT ... FOR UPDATE SKIP
LOCKED``), ``promote_claim_to_sandbox`` (``FOR UPDATE``, held across one
multi-table commit), and the reconciler's per-tick ``pg_try_advisory_lock``/
``pg_advisory_unlock`` pair - all depend on a statement's lock and a later
statement's commit or unlock observing the *same* backend session. A
connection pooler in statement- or transaction-pooling mode (e.g. PgBouncer
in its ``transaction`` pool_mode, or most managed "serverless"/proxy
Postgres front-ends) can hand different statements of what the application
sees as one session to different backend sessions, silently breaking every
one of those guarantees without raising an error. The pool's PostgreSQL
connection - whether it is the same Enterprise application database or a
separate bring-your-own instance under the existing Enterprise DB contract -
must terminate in session-pinned connections (direct connections, or a
pooler in ``session`` pool mode) for the sessions that run
``do_mars_pool_store``/``do_mars_pool_reconciler`` code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class PoolConfigError(ValueError):
    pass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    if raw.lower() not in ('true', 'false'):
        raise PoolConfigError(f'{name} must be "true" or "false"')
    return raw.lower() == 'true'


def _env_int(name: str, default: int, *, ge: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise PoolConfigError(f'{name} must be an integer') from None
    if value < ge:
        raise PoolConfigError(f'{name} must be >= {ge}')
    return value


@dataclass(frozen=True)
class DoMarsPoolConfig:
    enabled: bool = False
    # Scoped to exactly one Enterprise sandbox spec and its DO config, never
    # a wildcard: a spec encodes the MARS environment config, image and
    # bootstrap key, so pool inventory is only ever interchangeable within
    # one fully-matching spec/config pair.
    sandbox_spec_id: str | None = None
    min_hot: int = 0
    min_paused_reserve: int = 0
    max_total: int = 0
    # Independent of min_hot/min_paused_reserve: an operator can flip this
    # off to freeze inventory size (stop creating new slots) without
    # changing the target numbers they intend to resume later. Existing
    # hot/paused/claimed slots are entirely unaffected either way.
    replenish_enabled: bool = True
    reconcile_interval_seconds: int = 30
    health_check_interval_seconds: int = 60
    health_freshness_seconds: int = 120
    max_slot_age_seconds: int = 6 * 60 * 60
    boot_timeout_seconds: int = 300
    claim_timeout_seconds: int = 120
    max_concurrent_creates_per_tick: int = 1
    max_concurrent_pauses_per_tick: int = 2

    def __post_init__(self) -> None:
        if not self.enabled:
            return
        if not self.sandbox_spec_id:
            raise PoolConfigError(
                'DO_MARS_POOL_SANDBOX_SPEC_ID is required when the pool is enabled'
            )
        if self.min_hot < 0 or self.min_paused_reserve < 0:
            raise PoolConfigError('pool minimums must be >= 0')
        floor = self.min_hot + self.min_paused_reserve
        if self.max_total < floor:
            raise PoolConfigError(
                'DO_MARS_POOL_MAX_TOTAL must be >= min_hot + min_paused_reserve '
                f'({floor}), got {self.max_total}'
            )
        for name, value in (
            ('reconcile_interval_seconds', self.reconcile_interval_seconds),
            ('health_check_interval_seconds', self.health_check_interval_seconds),
            ('health_freshness_seconds', self.health_freshness_seconds),
            ('max_slot_age_seconds', self.max_slot_age_seconds),
            ('boot_timeout_seconds', self.boot_timeout_seconds),
            ('claim_timeout_seconds', self.claim_timeout_seconds),
        ):
            if value <= 0:
                raise PoolConfigError(f'{name} must be > 0')
        if self.max_concurrent_creates_per_tick < 1:
            raise PoolConfigError('max_concurrent_creates_per_tick must be >= 1')

    @staticmethod
    def from_env() -> 'DoMarsPoolConfig':
        enabled = _env_bool('DO_MARS_POOL_ENABLED', False)
        min_hot = _env_int('DO_MARS_POOL_MIN_HOT', 0)
        min_paused_reserve = _env_int('DO_MARS_POOL_MIN_PAUSED_RESERVE', 0)
        default_max = min_hot + min_paused_reserve
        return DoMarsPoolConfig(
            enabled=enabled,
            sandbox_spec_id=os.getenv('DO_MARS_POOL_SANDBOX_SPEC_ID') or None,
            min_hot=min_hot,
            min_paused_reserve=min_paused_reserve,
            max_total=_env_int('DO_MARS_POOL_MAX_TOTAL', default_max),
            replenish_enabled=_env_bool('DO_MARS_POOL_REPLENISH_ENABLED', True),
            reconcile_interval_seconds=_env_int(
                'DO_MARS_POOL_RECONCILE_INTERVAL_SECONDS', 30, ge=1
            ),
            health_check_interval_seconds=_env_int(
                'DO_MARS_POOL_HEALTH_CHECK_INTERVAL_SECONDS', 60, ge=1
            ),
            health_freshness_seconds=_env_int(
                'DO_MARS_POOL_HEALTH_FRESHNESS_SECONDS', 120, ge=1
            ),
            max_slot_age_seconds=_env_int(
                'DO_MARS_POOL_MAX_SLOT_AGE_SECONDS', 6 * 60 * 60, ge=1
            ),
            boot_timeout_seconds=_env_int('DO_MARS_POOL_BOOT_TIMEOUT_SECONDS', 300, ge=1),
            claim_timeout_seconds=_env_int(
                'DO_MARS_POOL_CLAIM_TIMEOUT_SECONDS', 120, ge=1
            ),
            max_concurrent_creates_per_tick=_env_int(
                'DO_MARS_POOL_MAX_CONCURRENT_CREATES', 1, ge=1
            ),
            max_concurrent_pauses_per_tick=_env_int(
                'DO_MARS_POOL_MAX_CONCURRENT_PAUSES', 2, ge=1
            ),
        )
