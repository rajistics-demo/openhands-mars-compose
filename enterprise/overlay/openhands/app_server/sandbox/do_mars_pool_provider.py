"""The provider seam the pool reconciler depends on.

``PoolProvider`` is a small Protocol, not the full ``DoMarsClient``: it names
only the provider operations a pool ever needs, so a reconciler unit test can
supply a fully synthetic fake instead of a real network/DO dependency, and a
future pool for a different backend can satisfy the same Protocol without
any change to the reconciler. ``DoMarsPoolProviderAdapter`` is the one
production implementation, and it does not reimplement provider access: it
is a thin wrapper around the same ``DoMarsClient`` and ``tunnel_http_request``
helper already used by ``DoMarsSandboxService``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from openhands.app_server.sandbox.do_mars_client import (
    SESSION_STATUS_PAUSED,
    DoMarsClient,
    DoMarsSession,
)
from openhands.app_server.sandbox.do_mars_tunnel import tunnel_http_request


class PoolProvider(Protocol):
    async def create_session(self, name: str, config_id: str) -> DoMarsSession: ...

    async def get_session(self, session_id: str) -> DoMarsSession | None: ...

    async def list_sessions_by_name(self, name: str) -> list[DoMarsSession]: ...

    async def pause_session(self, session_id: str) -> None: ...

    async def resume_session(self, session_id: str) -> None: ...

    async def delete_session(self, session_id: str) -> None: ...

    async def probe_dormant(self, session_id: str) -> bool:
        """True only if the guest's deferred-init Agent Server is reachable
        and reports ``dormant`` - i.e. booted, but never yet given tenant
        session keys or secrets. Dials the VM: only ever called on a session
        the caller just created or already knows is RUNNING, never on a
        paused or unclaimed-but-unconfirmed one."""
        ...

    async def probe_alive(self, session_id: str) -> bool:
        """Authenticated liveness probe for an already-HOT_READY (running,
        unclaimed, still dormant) slot. Never called on a paused slot: a
        port-forward dial can itself resume a paused MARS VM, so paused
        inventory is health-checked through ``get_session`` status only
        (see do_mars_pool_reconciler.health_check_paused)."""
        ...

    async def is_still_paused(self, session_id: str) -> bool:
        """Control-plane status read only - no tunnel, no dial, no wake.

        Confirms a PAUSED_RESERVE slot is still paused and still exists.
        This is the *only* permitted health signal for paused inventory.
        """
        ...


@dataclass
class DoMarsPoolProviderAdapter:
    """Production ``PoolProvider`` over the real DO MARS control plane."""

    do: DoMarsClient
    do_team_token: str

    async def create_session(self, name: str, config_id: str) -> DoMarsSession:
        return await self.do.create_session(name, config_id)

    async def get_session(self, session_id: str) -> DoMarsSession | None:
        return await self.do.get_session(session_id)

    async def list_sessions_by_name(self, name: str) -> list[DoMarsSession]:
        return await self.do.list_sessions_by_name(name)

    async def pause_session(self, session_id: str) -> None:
        await self.do.pause_session(session_id)

    async def resume_session(self, session_id: str) -> None:
        await self.do.resume_session(session_id)

    async def delete_session(self, session_id: str) -> None:
        await self.do.delete_session(session_id)

    async def probe_dormant(self, session_id: str) -> bool:
        tunnel_url = self.do.port_forward_url(session_id, 8000)
        probe = await tunnel_http_request(
            tunnel_url,
            do_team_token=self.do_team_token,
            http_host='localhost',
            method='GET',
            path='/api/init',
            headers={},
        )
        if probe.status_code != 200:
            return False
        try:
            return json.loads(probe.body).get('state') == 'dormant'
        except (ValueError, AttributeError):
            return False

    async def probe_alive(self, session_id: str) -> bool:
        # A hot, unclaimed slot has no session key yet (it is still
        # dormant), so its liveness signal is the same dormant probe used
        # at boot, not an authenticated /alive call.
        return await self.probe_dormant(session_id)

    async def is_still_paused(self, session_id: str) -> bool:
        session = await self.do.get_session(session_id)
        return session is not None and session.status == SESSION_STATUS_PAUSED
