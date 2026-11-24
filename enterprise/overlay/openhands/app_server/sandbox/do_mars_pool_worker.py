"""Standalone entrypoint for the opt-in DO MARS warm-pool reconciler.

Deliberately a *separate process* from the Agent Server app, not a
background task started inside a per-request injector (``inject()`` on
``DoMarsSandboxServiceInjector`` runs once per request, never once per
process, so it is the wrong place to start a long-lived loop). This mirrors
the "app vs worker instances" split the pool's own safety design already
assumes (see do_mars_pool_store.claim_one's docstring on cross-instance
claim safety): the app serves requests and performs claims inline via
``do_mars_pool_store.claim_one``/``promote_claim_to_sandbox``; this worker
is the one process that ticks the reconciler, and is safe to run as zero,
one, or several replicas because every tick is itself serialized by the
Postgres advisory lock in do_mars_pool_reconciler (``_try_tick_lock``) - a
second replica (or a second instance of this same process) simply finds the
lock held and skips that tick.

Intentionally stdlib/Enterprise-only, no new third-party dependency: reuses
the same ``DoMarsClient``/``get_do_mars_client`` and ``a_session_maker`` the
app already depends on.
"""

import asyncio
import logging
import os
import signal

logger = logging.getLogger(__name__)


async def run_forever() -> None:
    from openhands.app_server.sandbox.do_mars_client import (
        DO_MARS_BACKEND,
        get_do_mars_client,
    )
    from openhands.app_server.sandbox.do_mars_pool_config import (
        DoMarsPoolConfig,
        PoolConfigError,
    )
    from openhands.app_server.sandbox.do_mars_pool_provider import (
        DoMarsPoolProviderAdapter,
    )
    from openhands.app_server.sandbox.do_mars_pool_reconciler import DoMarsPoolReconciler

    try:
        config = DoMarsPoolConfig.from_env()
    except PoolConfigError as exc:
        raise SystemExit(f'do_mars_pool_worker: invalid pool configuration: {exc}')

    if not config.enabled:
        logger.info('do_mars_pool_worker.disabled_exiting')
        return

    # The *provider's* config id (the same DO_MARS_CONFIG_ID every non-pool
    # create_session call uses - do_mars_spec_service.py), never a
    # separately-settable pool-only label. Read and validated before any
    # Enterprise-core import below so a misconfigured worker fails fast the
    # same way an invalid DoMarsPoolConfig does, and so pool inventory is
    # always keyed by the config the provider actually used to create each
    # VM: if an operator rotates DO_MARS_CONFIG_ID, slots created under the
    # old value simply stop matching claim_one's filter in
    # do_mars_sandbox_service.py instead of being silently reused.
    config_id = os.environ.get('DO_MARS_CONFIG_ID')
    if not config_id:
        raise SystemExit(
            'do_mars_pool_worker: DO_MARS_CONFIG_ID is required when the pool '
            'is enabled (it must match the provider config set in site.env, '
            'not a separate pool-only value)'
        )

    # Local import: storage.database is Enterprise-core, not part of this
    # overlay, and must not be imported at module scope when
    # DO_MARS_POOL_ENABLED is unset (e.g. a plain import-check in a unit
    # test environment that lacks the full Enterprise tree).
    from storage.database import a_session_maker

    import httpx

    from openhands.app_server.sandbox.do_mars_secrets import read_secret

    do_team_token = read_secret('DO_MARS_TEAM_TOKEN')

    async with httpx.AsyncClient() as http:
        provider = DoMarsPoolProviderAdapter(
            do=get_do_mars_client(do_team_token, http), do_team_token=do_team_token
        )
        reconciler = DoMarsPoolReconciler(
            session_factory=a_session_maker,
            provider=provider,
            config=config,
            backend=DO_MARS_BACKEND,
            config_id=config_id,
        )

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)

        logger.info(
            'do_mars_pool_worker.started',
            extra={
                'min_hot': config.min_hot,
                'min_paused_reserve': config.min_paused_reserve,
                'max_total': config.max_total,
                'interval_seconds': config.reconcile_interval_seconds,
            },
        )
        while not stop.is_set():
            try:
                report = await reconciler.run_once()
                if report.errors:
                    logger.warning('do_mars_pool_worker.tick_had_errors', extra={'errors': report.errors})
            except Exception:  # noqa: BLE001 - one bad tick must not kill the worker
                logger.exception('do_mars_pool_worker.tick_crashed')
            try:
                await asyncio.wait_for(stop.wait(), timeout=config.reconcile_interval_seconds)
            except TimeoutError:
                pass
        logger.info('do_mars_pool_worker.stopped')


def main() -> None:
    logging.basicConfig(level=os.environ.get('DO_MARS_POOL_LOG_LEVEL', 'INFO'))
    asyncio.run(run_forever())


if __name__ == '__main__':
    main()
