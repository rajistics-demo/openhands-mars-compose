"""App-owned DO MARS sandboxes, initialized through the Agent Server API.

Create uses a row-first, intent-before-provider-call pattern (D4): the row
is written with ``provider_create_intent_at`` set and ``provider_resource_id``
still NULL, then the DO session is created. On success the resource id is
written immediately. On an ambiguous outcome (timeout, 5xx after the request
was sent, or a 409 saying the name already exists) the row is reconciled by
listing DO sessions by name and adopting only an exact single match created
after the intent timestamp with the expected config - never by a second
create. A row that cannot be resolved this way is left with intent set and no
resource id, discoverable by ``get_pending_create_intents`` for the operator
reconciliation path; it is never adopted speculatively and never replays the
create.
"""

import asyncio
import json
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, AsyncGenerator, ClassVar
from urllib.parse import urlsplit

import httpx
from fastapi import Request
from pydantic import Field, SecretStr

from openhands.agent_server.utils import utc_now
from openhands.app_server.errors import SandboxDeleteRetryError, SandboxError
from openhands.app_server.sandbox.do_mars_client import (
    DO_MARS_BACKEND,
    SESSION_STATUS_PAUSED,
    SESSION_STATUS_READY,
    DoMarsClient,
    DoMarsSession,
    get_do_mars_client,
)
from openhands.app_server.sandbox.do_mars_credentials import (
    issue_session_key,
    valid_session_key,
)
from openhands.app_server.sandbox.do_mars_diagnostics import (
    error_class,
    log_diagnostic,
)
from openhands.app_server.sandbox.do_mars_pool_config import DoMarsPoolConfig
from openhands.app_server.sandbox.do_mars_pool_models import PoolSlotState
from openhands.app_server.sandbox.do_mars_pool_store import (
    claim_one,
    promote_claim_to_sandbox,
)
from openhands.app_server.sandbox.do_mars_preview import (
    PREVIEW_PORT,
    preview_domain,
    preview_origin,
)
from openhands.app_server.sandbox.do_mars_secrets import read_secret
from openhands.app_server.sandbox.do_mars_spec_service import DoMarsSpecInfo
from openhands.app_server.sandbox.do_mars_tunnel import tunnel_http_request
from openhands.app_server.sandbox.managed_sandbox_service import (
    ManagedSandboxService,
    ManagedSandboxServiceInjector,
    ProviderOutcome,
)
from openhands.app_server.sandbox.sandbox_models import (
    AGENT_SERVER,
    WORKER_1,
    WORKER_2,
    ExposedUrl,
    SandboxInfo,
    SandboxPage,
    SandboxRecord,
    SandboxStartedBy,
    SandboxStatus,
)
from openhands.app_server.sandbox.sandbox_service import SandboxService
from openhands.app_server.sandbox.sandbox_spec_service import (
    SandboxSpecService,
    get_agent_server_env,
    resolve_sandbox_spec,
)
from openhands.app_server.sandbox.sandbox_store import (
    StoredSandbox,
    get_credential_org_id,
    get_stored_sandbox_by_session_api_key,
    hash_session_api_key,
    require_user_id,
    sandbox_started_by,
    search_stored_sandboxes,
)
from openhands.app_server.services.injector import InjectorState

logger = logging.getLogger(__name__)

# 8000 is the Agent Server, the rest match the E2B/AWS port convention.
PORTS = (8000, 8001, 8011, 8012)


def _status_for(session: DoMarsSession | None) -> SandboxStatus:
    """Unknown provider states fail closed instead of appearing ready."""
    if session is None or session.status in {
        'SESSION_STATUS_DESTROYED',
        'SESSION_STATUS_DELETED',
    }:
        return SandboxStatus.MISSING
    if session.status == SESSION_STATUS_READY:
        return SandboxStatus.RUNNING
    if session.status == SESSION_STATUS_PAUSED:
        return SandboxStatus.PAUSED
    if session.status in {
        'SESSION_STATUS_CREATING',
        'SESSION_STATUS_STARTING',
        'SESSION_STATUS_PENDING',
        'SESSION_STATUS_PAUSING',
        'SESSION_STATUS_RESUMING',
    }:
        return SandboxStatus.STARTING
    return SandboxStatus.ERROR


@dataclass
class DoMarsSandboxService(ManagedSandboxService):
    backend: ClassVar[str] = DO_MARS_BACKEND

    sandbox_spec_service: SandboxSpecService
    do: DoMarsClient
    do_bearer_token: str
    gateway_base_url: str
    callback_url: str
    startup_timeout_seconds: int = 300
    web_url: str | None = None
    internal_url: str | None = None
    preview_domain: str | None = None
    # Opt-in warm pool (see do_mars_pool_config.py). None/disabled leaves
    # start_sandbox's create path byte-for-byte what it was before this
    # field existed: the pool-claim branch below never runs unless an
    # operator both constructs this with a config AND sets
    # ``DO_MARS_POOL_ENABLED=true`` for this exact spec.
    pool_config: DoMarsPoolConfig | None = None

    def exposed_urls(self, row: StoredSandbox) -> list[ExposedUrl]:
        urls = [
            ExposedUrl(
                name=AGENT_SERVER,
                port=8000,
                url=f'{self.gateway_base_url.rstrip("/")}/{row.id}/8000',
            )
        ]
        if self.preview_domain:
            urls.append(
                ExposedUrl(
                    name=WORKER_1,
                    port=PREVIEW_PORT,
                    url=preview_origin(self.preview_domain, row.id) + '/',
                )
            )
        return urls

    async def _get_session(self, row: StoredSandbox) -> DoMarsSession | None:
        """The DO session for a row, or None when there is none to read yet.

        A row whose create is still unresolved (``provider_resource_id`` is
        NULL) has nothing to poll at DO: it is reported as STARTING by the
        caller without a DO round trip, rather than querying with a made-up
        id.
        """
        if not row.provider_resource_id:
            return None
        return await self.do.get_session(row.provider_resource_id)

    async def as_info(
        self, row: StoredSandbox, *, tolerate_status_failure: bool = False
    ) -> SandboxInfo:
        status_detail = None
        if not row.provider_resource_id:
            status = SandboxStatus.STARTING
            status_detail = 'DigitalOcean MARS session create has not resolved yet'
        else:
            try:
                session = await self._get_session(row)
            except SandboxError:
                if not tolerate_status_failure:
                    raise
                # History stays readable without DO. An unavailable control
                # plane is never mistaken for proof the VM was deleted, and
                # no key is exposed while status is unknown.
                session = None
                status_detail = 'DigitalOcean MARS status is temporarily unavailable'
                status = SandboxStatus.STARTING
            else:
                status = _status_for(session)
                if status == SandboxStatus.MISSING:
                    status_detail = 'DigitalOcean MARS session is gone or terminated'
        return SandboxInfo(
            supports_runtime_pause=True,
            id=row.id,
            created_by_user_id=row.created_by_user_id,
            credential_org_id=row.credential_org_id,
            started_by=sandbox_started_by(row),
            sandbox_spec_id=row.sandbox_spec_id,
            status=status,
            session_api_key=row.session_api_key.get_secret_value()
            if row.session_api_key and status == SandboxStatus.RUNNING
            else None,
            exposed_urls=self.exposed_urls(row)
            if status == SandboxStatus.RUNNING
            else [],
            created_at=row.created_at,
            status_detail=status_detail,
        )

    async def batch_get_sandboxes(
        self, sandbox_ids: list[str]
    ) -> list[SandboxInfo | None]:
        """Enrich saved conversations without making history depend on DO.

        Only batch display reads tolerate provider failures. Single reads and
        capacity/lifecycle operations still propagate errors and fail closed.
        """

        # One AsyncSession cannot run concurrent operations, so authorized
        # rows are read serially; only the provider status reads overlap.
        rows = [await self._get_stored_sandbox(sid) for sid in sandbox_ids]

        async def display_info(row: StoredSandbox | None) -> SandboxInfo | None:
            return (
                await self.as_info(row, tolerate_status_failure=True) if row else None
            )

        return list(await asyncio.gather(*(display_info(row) for row in rows)))

    async def search_sandboxes(
        self, page_id: str | None = None, limit: int = 100
    ) -> SandboxPage:
        page = await search_stored_sandboxes(
            self.db_session, self.user_context, self.backend, page_id, limit
        )
        return SandboxPage(
            items=[await self.as_info(row) for row in page.items],
            next_page_id=page.next_page_id,
        )

    async def get_sandbox(self, sandbox_id: str) -> SandboxInfo | None:
        row = await self._get_stored_sandbox(sandbox_id)
        return await self.as_info(row) if row else None

    async def _row_for_valid_session_key(
        self, session_api_key: str
    ) -> StoredSandbox | None:
        """The row only while the key is unexpired and bound to its VM.

        The stored hash proves the key was issued for this row; the signed
        claims add expiry and the provider binding, so an expired key cannot
        read secrets or post callbacks even though its hash still matches.
        """
        row = await get_stored_sandbox_by_session_api_key(
            self.db_session, self.user_context, self.backend, session_api_key
        )
        if (
            row is None
            or not row.provider_resource_id
            or not valid_session_key(session_api_key, row.id, row.provider_resource_id)
        ):
            return None
        return row

    async def get_sandbox_by_session_api_key(
        self, session_api_key: str
    ) -> SandboxInfo | None:
        row = await self._row_for_valid_session_key(session_api_key)
        return await self.as_info(row) if row else None

    async def get_sandbox_record_by_session_api_key(
        self, session_api_key: str
    ) -> SandboxRecord | None:
        row = await self._row_for_valid_session_key(session_api_key)
        if row is None:
            return None
        return SandboxRecord(
            id=row.id,
            created_by_user_id=row.created_by_user_id,
            credential_org_id=row.credential_org_id,
        )

    async def _resolve_ambiguous_create(
        self, row: StoredSandbox, name: str, config_id: str, intent_at: datetime
    ) -> DoMarsSession:
        """Adopt a session for ``row`` after a create whose result is unknown.

        Adopts only when exactly one DO session has this exact name, the
        expected config, a creation time at or after the intent timestamp,
        and is not already the resource id of another row. Any other
        outcome - zero matches, more than one, a config mismatch, or a
        session mapped elsewhere - fails closed: nothing is adopted and
        there is no second create. The caller leaves the row as-is (intent
        set, no resource id) for the reconciliation path.
        """
        candidates = await self.do.list_sessions_by_name(name)
        matches = [c for c in candidates if c.config_id == config_id]
        if len(matches) != 1:
            raise SandboxError(
                'DigitalOcean MARS create result is ambiguous: '
                f'{len(matches)} session(s) named {name!r} with the expected config'
            )
        match = matches[0]
        try:
            created = datetime.fromisoformat(
                (match.created_at or '').replace('Z', '+00:00')
            )
            if created.tzinfo is None or created < intent_at:
                raise ValueError('Session predates intent or has no timezone')
        except ValueError as exc:
            raise SandboxError('MARS create cannot be safely reconciled') from exc
        already_mapped = await self.db_session.scalar(
            self._select_by_resource_id(match.id)
        )
        if already_mapped is not None and already_mapped.id != row.id:
            raise SandboxError(
                'DigitalOcean MARS session is already mapped to another sandbox'
            )
        return match

    def _select_by_resource_id(self, resource_id: str) -> Any:
        from sqlalchemy import select

        return select(StoredSandbox).where(
            StoredSandbox.backend == self.backend,
            StoredSandbox.provider_resource_id == resource_id,
        )

    async def wait_ready(self, session_id: str) -> DoMarsSession:
        deadline = asyncio.get_event_loop().time() + self.startup_timeout_seconds
        while asyncio.get_event_loop().time() < deadline:
            session = await self.do.get_session(session_id)
            if session is None:
                raise SandboxError('DigitalOcean MARS session is no longer available')
            if session.status == SESSION_STATUS_READY:
                return session
            if _status_for(session) in (SandboxStatus.ERROR, SandboxStatus.MISSING):
                raise SandboxError('MARS session entered a terminal or unknown state')
            await asyncio.sleep(2)
        raise SandboxError('DigitalOcean MARS session did not reach READY in time')

    async def start_sandbox(
        self,
        sandbox_spec_id: str | None = None,
        sandbox_id: str | None = None,
        extra_env: dict[str, str] | None = None,
        started_by: SandboxStartedBy = SandboxStartedBy.SANDBOX_API,
    ) -> SandboxInfo:
        user = await require_user_id(self.user_context)
        org = await get_credential_org_id(self.user_context)
        spec = await resolve_sandbox_spec(
            sandbox_spec_id,
            await self.user_context.get_default_sandbox_spec_id(),
            self.sandbox_spec_service,
            logger,
        )
        if not isinstance(spec, DoMarsSpecInfo):
            raise SandboxError('A DigitalOcean MARS spec is required')
        if not spec.init_api_key.get_secret_value() or not self.callback_url.startswith(
            'https://'
        ):
            raise SandboxError(
                'DigitalOcean MARS init key and HTTPS callback URL are required'
            )
        await self.pause_old_sandboxes(self.max_num_sandboxes - 1)

        if (
            self.pool_config
            and self.pool_config.enabled
            and self.pool_config.sandbox_spec_id == spec.id
        ):
            claimed = await self._start_sandbox_from_pool(
                spec, sandbox_id, started_by, extra_env, user, org
            )
            if claimed is not None:
                return claimed
            # No eligible HOT_READY slot (empty, cold, or only paused
            # inventory the service does not resume inline - see
            # claim_one's ``states`` docstring): fall through to the exact
            # unmodified create path below, same as if the pool were
            # disabled.

        sid = sandbox_id or secrets.token_hex(16)
        name = f'oh-{sid}'
        key = secrets.token_urlsafe(32)
        intent_at = utc_now()
        row = self._new_stored_sandbox(
            id=sid,
            created_by_user_id=user,
            credential_org_id=org,
            started_by=started_by.value,
            sandbox_spec_id=spec.id,
            session_api_key=SecretStr(key),
            session_api_key_hash=hash_session_api_key(key),
            provider_create_intent_at=intent_at,
            provider_resource_id=None,
        )
        self.db_session.add(row)
        await self.db_session.commit()

        try:
            try:
                session = await self.do.create_session(name, spec.config_id)
            except LookupError:
                # The provider reports the name already exists. This can
                # mean the create actually landed, or a concurrent/retried
                # attempt did. Either way, no second create: resolve by name.
                session = await self._resolve_ambiguous_create(
                    row, name, spec.config_id, intent_at
                )
            except SandboxError:
                # Timeout or a 5xx after the request may already have been
                # sent to DO. Try the same single-match adoption before
                # giving up; a transient list failure still fails closed.
                session = await self._resolve_ambiguous_create(
                    row, name, spec.config_id, intent_at
                )

            row.provider_resource_id = session.id
            key = issue_session_key(row.id, session.id)
            row.session_api_key = SecretStr(key)
            row.session_api_key_hash = hash_session_api_key(key)
            await self.db_session.commit()

            session = await self.wait_ready(session.id)
            await self._init_agent_server(row, session, spec, extra_env)
        except BaseException:
            # Shielded cleanup: a crash mid-init must not leave a DO session
            # with no row ever pointing at it. The row itself is left for the
            # caller/lifecycle to delete-retry rather than removed here,
            # because an unconfirmed delete must never silently drop a live
            # sandbox from history.
            if row.provider_resource_id:
                try:
                    await asyncio.shield(
                        self.do.delete_session(row.provider_resource_id)
                    )
                except Exception as cleanup_exc:
                    log_diagnostic(
                        'do_mars.cleanup_after_init_failure_failed',
                        level=logging.ERROR,
                        operation='session.delete',
                        error_class=error_class(cleanup_exc),
                    )
            raise
        return await self.as_info(row)

    async def _start_sandbox_from_pool(
        self,
        spec: DoMarsSpecInfo,
        sandbox_id: str | None,
        started_by: SandboxStartedBy,
        extra_env: dict[str, str] | None,
        user: str,
        org: str | None,
    ) -> SandboxInfo | None:
        """Claim a pre-created HOT_READY slot instead of a fresh DO create.

        Returns ``None`` only for "no eligible slot was claimed" - the one
        case where ``start_sandbox`` may still fall through to its normal
        create path. ``claim_one`` either claims a slot and commits that
        claim, or claims nothing and rolls back (do_mars_pool_store.py); it
        never leaves a half-claimed row for this method to reason about.
        Once ``promote_claim_to_sandbox`` below commits, this VM is a real,
        owned ``StoredSandbox`` row like any other, so every failure past
        that point is raised (never swallowed into a ``None`` return) and
        goes through the exact same shielded delete-and-reraise cleanup
        ``start_sandbox``'s own create path uses - this is deliberately not
        a second, divergent cleanup implementation.

        HOT_READY only (``states=(PoolSlotState.HOT_READY,)``): this method
        never resumes a paused VM inline, so it never waits on a resume
        inside a tenant-facing create call. Rebalancing paused inventory
        up to hot is entirely the reconciler's job, on its own schedule.
        """

        sid = sandbox_id or secrets.token_hex(16)
        slot = await claim_one(
            self.db_session,
            backend=DO_MARS_BACKEND,
            sandbox_spec_id=spec.id,
            # The *provider's* config id for this exact spec, never a
            # separately-settable pool label: a pool slot's config_id is
            # the DO environment config it was actually created under, so
            # keying the claim query on spec.config_id (the same value
            # start_sandbox's own create path passes to do.create_session
            # a few lines up) means a slot created under a config that has
            # since been rotated out of this spec is never claimable -
            # the row simply stops matching, with no extra validation
            # needed. A previous revision used an independent
            # ``DO_MARS_POOL_CONFIG_ID`` label defaulting to the literal
            # string "default", which could silently diverge from the
            # provider's real config id and let a stale-config VM be
            # claimed for a spec now pointing at a different config.
            config_id=spec.config_id,
            claim_owner_sandbox_id=sid,
            claimed_by_user_id=user,
            claimed_org_id=org,
            min_health_check_after=utc_now()
            - timedelta(seconds=self.pool_config.health_freshness_seconds),
            states=(PoolSlotState.HOT_READY,),
        )
        if slot is None:
            return None

        key = secrets.token_urlsafe(32)
        row = self._new_stored_sandbox(
            id=sid,
            created_by_user_id=user,
            credential_org_id=org,
            started_by=started_by.value,
            sandbox_spec_id=spec.id,
            session_api_key=SecretStr(key),
            session_api_key_hash=hash_session_api_key(key),
            provider_create_intent_at=None,
            provider_resource_id=slot.provider_resource_id,
        )

        async def _create_row(db_session: Any) -> None:
            db_session.add(row)

        await promote_claim_to_sandbox(
            self.db_session,
            slot_id=slot.id,
            claim_owner_sandbox_id=sid,
            create_sandbox_row=_create_row,
        )

        # From here on this is an owned sandbox like any other: a failure
        # must be raised, not swallowed, and cleaned up the same way.
        try:
            key = issue_session_key(row.id, slot.provider_resource_id)
            row.session_api_key = SecretStr(key)
            row.session_api_key_hash = hash_session_api_key(key)
            await self.db_session.commit()

            session = await self.wait_ready(slot.provider_resource_id)
            await self._init_agent_server(row, session, spec, extra_env)
        except BaseException:
            if row.provider_resource_id:
                try:
                    await asyncio.shield(
                        self.do.delete_session(row.provider_resource_id)
                    )
                except Exception as cleanup_exc:
                    log_diagnostic(
                        'do_mars.cleanup_after_init_failure_failed',
                        level=logging.ERROR,
                        operation='session.delete',
                        error_class=error_class(cleanup_exc),
                    )
            raise
        return await self.as_info(row)

    async def _init_agent_server(
        self,
        row: StoredSandbox,
        session: DoMarsSession,
        spec: DoMarsSpecInfo,
        extra_env: dict[str, str] | None,
    ) -> None:
        tunnel_url = self.do.port_forward_url(session.id, 8000)
        probe = await tunnel_http_request(
            tunnel_url,
            do_bearer_token=self.do_bearer_token,
            http_host='localhost',
            method='GET',
            path='/api/init',
            headers={},
        )
        if probe.status_code != 200:
            log_diagnostic(
                'do_mars.init_failed',
                operation='init.probe',
                reason='bad_status',
                status_code=probe.status_code,
            )
            raise SandboxError('DO MARS Agent Server init probe failed')
        if json.loads(probe.body).get('state') != 'dormant':
            log_diagnostic(
                'do_mars.init_failed', operation='init.probe', reason='not_dormant'
            )
            raise SandboxError('DO MARS Agent Server init probe failed')

        key = row.session_api_key.get_secret_value() if row.session_api_key else ''
        body = {
            'session_api_keys': [key],
            'secret_key': secrets.token_urlsafe(32),
            'conversations_path': '/workspace/.openhands/conversations',
            'bash_events_dir': '/workspace/.openhands/bash_events',
            'conversation_worktree_root': '/workspace/worktrees',
            'env': {
                WORKER_1: '8011',
                WORKER_2: '8012',
                'OH_VSCODE_BASE_PATH': (
                    f'{urlsplit(self.gateway_base_url).path}/{row.id}/8001'
                ),
                'TMUX_TMPDIR': '/tmp',
                **spec.initial_env,
                **get_agent_server_env(),
                **self._lifecycle_env(),
                **(extra_env or {}),
                'OH_SESSION_API_KEYS_0': key,
            },
            'allow_cors_origins': [
                self.web_url
                or (
                    f'{urlsplit(self.gateway_base_url).scheme}://'
                    f'{urlsplit(self.gateway_base_url).netloc}'
                )
            ],
            'webhooks': [
                {'base_url': f'{self.callback_url.rstrip("/")}/api/v1/webhooks'}
            ],
        }
        payload = json.dumps(body).encode()
        response = await tunnel_http_request(
            tunnel_url,
            do_bearer_token=self.do_bearer_token,
            http_host='localhost',
            method='POST',
            path='/api/init',
            headers={
                'Content-Type': 'application/json',
                'X-Init-API-Key': spec.init_api_key.get_secret_value(),
            },
            body=payload,
        )
        if response.status_code != 200:
            log_diagnostic(
                'do_mars.init_failed',
                operation='init.post',
                reason='bad_status',
                status_code=response.status_code,
            )
            raise SandboxError('DO MARS Agent Server initialization failed')

    async def _resume_at_provider(self, row: StoredSandbox) -> ProviderOutcome:
        if not row.provider_resource_id:
            return ProviderOutcome.SKIPPED
        session = await self.do.get_session(row.provider_resource_id)
        if session is None:
            return ProviderOutcome.FAILED
        if session.status == SESSION_STATUS_READY:
            return ProviderOutcome.ALREADY_DONE
        if session.status != SESSION_STATUS_PAUSED:
            raise SandboxError(
                'DigitalOcean MARS session is not resumable in its current state',
                status_code=409,
            )
        if not row.session_api_key or not valid_session_key(
            row.session_api_key.get_secret_value(), row.id, row.provider_resource_id
        ):
            raise SandboxError(
                'MARS session credential expired; create a fresh sandbox',
                status_code=409,
            )
        await self.do.resume_session(row.provider_resource_id)
        await self.wait_ready(row.provider_resource_id)
        return ProviderOutcome.CHANGED

    async def _pause_at_provider(self, row: StoredSandbox) -> ProviderOutcome:
        if not row.provider_resource_id:
            return ProviderOutcome.SKIPPED
        session = await self.do.get_session(row.provider_resource_id)
        if session is None:
            return ProviderOutcome.FAILED
        if session.status == SESSION_STATUS_PAUSED:
            return ProviderOutcome.ALREADY_DONE
        if session.status != SESSION_STATUS_READY:
            return ProviderOutcome.SKIPPED
        await self.do.pause_session(row.provider_resource_id)
        deadline = asyncio.get_running_loop().time() + 60
        while asyncio.get_running_loop().time() < deadline:
            session = await self.do.get_session(row.provider_resource_id)
            if session is not None and session.status == SESSION_STATUS_PAUSED:
                return ProviderOutcome.CHANGED
            if _status_for(session) in (SandboxStatus.MISSING, SandboxStatus.ERROR):
                raise SandboxError('MARS pause did not reach PAUSED')
            await asyncio.sleep(1)
        raise SandboxError('MARS pause confirmation timed out')

    async def _delete_at_provider(self, row: StoredSandbox) -> None:
        reason: str | None = None
        try:
            if not row.provider_resource_id:
                spec = await self.sandbox_spec_service.get_sandbox_spec(
                    row.sandbox_spec_id
                )
                if (
                    not isinstance(spec, DoMarsSpecInfo)
                    or not row.provider_create_intent_at
                ):
                    reason = 'unresolved_intent'
                    log_diagnostic('do_mars.delete_unconfirmed', reason=reason)
                    raise SandboxError(
                        'MARS unresolved intent requires operator reconciliation'
                    )
                session = await self._resolve_ambiguous_create(
                    row, f'oh-{row.id}', spec.config_id, row.provider_create_intent_at
                )
                row.provider_resource_id = session.id
                await self.db_session.commit()
            await self.do.delete_session(row.provider_resource_id)
            deadline = asyncio.get_running_loop().time() + 60
            last_status = None
            while asyncio.get_running_loop().time() < deadline:
                remaining_session = await self.do.get_session(row.provider_resource_id)
                if _status_for(remaining_session) == SandboxStatus.MISSING:
                    return
                last_status = remaining_session.status if remaining_session else None
                await asyncio.sleep(1)
            reason = 'confirmation_timeout'
            log_diagnostic(
                'do_mars.delete_unconfirmed',
                reason=reason,
                provider_status=last_status,
            )
            raise SandboxError('MARS deletion confirmation timed out')
        except SandboxError as exc:
            if reason is None:
                log_diagnostic(
                    'do_mars.delete_unconfirmed',
                    reason='control_error',
                    error_class=error_class(exc.__cause__ or exc),
                )
            raise SandboxDeleteRetryError(
                'DigitalOcean MARS deletion is unconfirmed'
            ) from exc

    async def _check_agent_server_alive(
        self, sandbox: SandboxInfo, httpx_client: httpx.AsyncClient
    ) -> bool:
        """Readiness probe for ``wait_for_sandbox_running``.

        The gateway authenticates every sandbox route, ``/alive`` included,
        so the stock anonymous probe can never succeed here. Without a key
        no probe is sent at all.
        """
        if not sandbox.session_api_key:
            return False
        try:
            url = self._get_agent_server_url(sandbox)
            response = await httpx_client.get(
                f'{url.rstrip("/")}/alive',
                headers={'X-Session-API-Key': sandbox.session_api_key},
                timeout=5.0,
            )
        except Exception as exc:
            log_diagnostic(
                'do_mars.health_probe_failed',
                level=logging.DEBUG,
                operation='health.probe',
                reason='transport_error',
                error_class=error_class(exc),
            )
            return False
        if not response.is_success:
            # 5xx is expected while the guest boots; a refused key is not.
            log_diagnostic(
                'do_mars.health_probe_failed',
                level=logging.WARNING
                if response.status_code in (401, 403)
                else logging.DEBUG,
                operation='health.probe',
                reason='bad_status',
                status_code=response.status_code,
            )
            return False
        return True

    async def get_idle_time(self, sandbox: SandboxInfo) -> float | None:
        if not sandbox.session_api_key:
            return None
        try:
            url = self._get_agent_server_url(sandbox)
            response = await self.httpx_client.get(
                f'{url.rstrip("/")}/server_info',
                headers={'X-Session-API-Key': sandbox.session_api_key},
                timeout=6,
            )
            if response.status_code != 200:
                log_diagnostic(
                    'do_mars.idle_probe_failed',
                    operation='idle.probe',
                    reason='bad_status',
                    status_code=response.status_code,
                )
                return None
            return float(response.json()['idle_time'])
        except (httpx.HTTPError, KeyError, TypeError, ValueError, SandboxError) as exc:
            log_diagnostic(
                'do_mars.idle_probe_failed',
                operation='idle.probe',
                reason='transport_error',
                error_class=error_class(exc),
            )
            return None


class DoMarsSandboxServiceInjector(ManagedSandboxServiceInjector):
    backend: ClassVar[str] = DO_MARS_BACKEND

    # The DO team token. Read only server-side (app, worker, gateway); never
    # put in a manifest, guest env, log or forwarded to a browser/guest.
    do_team_token: SecretStr = Field(
        default_factory=lambda: SecretStr(read_secret('DO_MARS_TEAM_TOKEN')),
        exclude=True,
    )
    gateway_base_url: str = Field(
        default_factory=lambda: os.environ['DO_MARS_GATEWAY_URL']
    )
    # Deliberately unset by default: unlike the Docker/Lambda internal_url
    # pattern, DO MARS has no docker-network-local shortcut for the public
    # gateway origin, and Automation's AUTOMATION_SANDBOX_INTERNAL_URL must
    # stay unset for this backend (4.12) so it is not rewritten to Docker.
    internal_url: str | None = Field(
        default_factory=lambda: os.getenv('DO_MARS_INTERNAL_URL')
    )
    # Admission pauses the owner's oldest RUNNING sandbox beyond this limit,
    # so concurrent runs by one automation identity need at least that many.
    max_num_sandboxes: int = Field(default=10, ge=1)
    startup_timeout_seconds: int = Field(default=300, ge=10, le=900)
    callback_url: str | None = Field(
        default_factory=lambda: os.getenv('DO_MARS_CALLBACK_URL')
    )
    # Disabled unless every DO_MARS_POOL_* setting opts in (see
    # do_mars_pool_config.py); DoMarsPoolConfig.from_env() never raises for
    # the all-unset/disabled case, only once DO_MARS_POOL_ENABLED=true.
    pool_config: DoMarsPoolConfig = Field(default_factory=DoMarsPoolConfig.from_env)

    async def inject(
        self, state: InjectorState, request: Request | None = None
    ) -> AsyncGenerator[SandboxService, None]:
        from openhands.app_server.config import (
            get_db_session,
            get_global_config,
            get_httpx_client,
            get_sandbox_spec_service,
            get_user_context,
        )

        async with (
            get_user_context(state, request) as user,
            get_httpx_client(state, request) as http,
            get_sandbox_spec_service(state, request) as specs,
            get_db_session(state, request) as db,
        ):
            yield DoMarsSandboxService(
                user_context=user,
                httpx_client=http,
                db_session=db,
                sandbox_spec_service=specs,
                do=get_do_mars_client(self.do_team_token.get_secret_value(), http),
                do_bearer_token=self.do_team_token.get_secret_value(),
                gateway_base_url=self.gateway_base_url,
                web_url=get_global_config().web_url,
                callback_url=self.callback_url or get_global_config().web_url or '',
                max_num_sandboxes=self.max_num_sandboxes,
                startup_timeout_seconds=self.startup_timeout_seconds,
                internal_url=self.internal_url,
                preview_domain=preview_domain(get_global_config().web_url or ''),
                lifecycle=self.lifecycle,
                pool_config=self.pool_config,
            )
