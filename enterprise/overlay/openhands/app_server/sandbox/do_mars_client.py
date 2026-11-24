"""Typed client for the DigitalOcean MARS (Harness Runtime) control API.

MARS is ``/v2/agents/*`` (sessions, configs), a different product from the
generic ``/v2/microvms`` API. The team token is read server-side only and
never put in a manifest, guest env, log or forwarded to a browser/guest.
"""

import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from openhands.app_server.errors import SandboxError
from openhands.app_server.sandbox.do_mars_diagnostics import (
    error_class,
    log_diagnostic,
)

DO_MARS_BACKEND = 'do-mars'
DO_API_ORIGIN = 'https://api.digitalocean.com'

# DO session ids are UUIDs; validated before being interpolated into any URL.
SESSION_ID_PATTERN = re.compile(
    r'\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z'
)

# DO Harness Runtime session statuses (4.4 of the design). Unknown/future
# values are left as strings and mapped to SandboxStatus.ERROR by the caller.
SESSION_STATUS_READY = 'SESSION_STATUS_READY'
SESSION_STATUS_PAUSED = 'SESSION_STATUS_PAUSED'


def validate_session_id(value: str) -> str:
    if not isinstance(value, str) or not SESSION_ID_PATTERN.fullmatch(value):
        raise ValueError('Invalid MARS session identifier')
    return value


@dataclass
class DoMarsSession:
    id: str
    name: str
    status: str
    config_id: str | None
    created_at: str | None


class DoMarsClient:
    """Bounded, redacted HTTP calls to the DO MARS control plane.

    The origin is fixed to ``DO_API_ORIGIN`` and redirects are never
    followed, so a DO-side redirect can never smuggle the team token to an
    attacker-controlled host.
    """

    def __init__(self, team_token: str, httpx_client: httpx.AsyncClient):
        self._token = team_token
        self._client = httpx_client

    def _headers(self) -> dict[str, str]:
        return {
            'Authorization': f'Bearer {self._token}',
            'Content-Type': 'application/json',
        }

    async def _request(
        self, operation: str, method: str, path: str, **kwargs: Any
    ) -> httpx.Response:
        url = f'{DO_API_ORIGIN}{path}'
        try:
            response = await self._client.request(
                method,
                url,
                headers=self._headers(),
                follow_redirects=False,
                timeout=30,
                **kwargs,
            )
        except httpx.HTTPError as exc:
            log_diagnostic(
                'do_mars.control_transport_error',
                operation=operation,
                method=method,
                error_class=error_class(exc),
            )
            raise SandboxError(
                'DigitalOcean MARS control request failed', status_code=502
            ) from exc
        if response.status_code >= 300:
            log_diagnostic(
                'do_mars.control_response',
                # A 404 is the expected end of delete confirmation polling.
                level=logging.INFO if response.status_code == 404 else logging.WARNING,
                operation=operation,
                method=method,
                status_code=response.status_code,
            )
        if response.is_redirect:
            # A credential-bearing request must never be replayed with a
            # redirect target chosen by the server.
            raise SandboxError('DigitalOcean MARS returned an unexpected redirect')
        return response

    @staticmethod
    def _as_session(data: dict[str, Any]) -> DoMarsSession:
        data = data.get('session', data)
        return DoMarsSession(
            id=validate_session_id(data.get('session', data)['session_id']),
            name=data.get('name', ''),
            status=data.get('status', ''),
            config_id=data.get('config_id'),
            created_at=data.get('created_at'),
        )

    async def create_session(self, name: str, config_id: str) -> DoMarsSession:
        response = await self._request(
            'session.create',
            'POST',
            '/v2/agents/sessions',
            json={'name': name, 'config_id': config_id},
        )
        if response.status_code == 409:
            raise LookupError(f'DigitalOcean MARS session {name!r} already exists')
        if response.status_code >= 400:
            raise SandboxError(
                'DigitalOcean MARS session create failed', status_code=502
            )
        return self._as_session(response.json())

    async def get_session(self, session_id: str) -> DoMarsSession | None:
        if not SESSION_ID_PATTERN.fullmatch(session_id):
            return None
        response = await self._request(
            'session.get', 'GET', f'/v2/agents/sessions/{session_id}'
        )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise SandboxError('DigitalOcean MARS session read failed', status_code=502)
        return self._as_session(response.json())

    async def list_sessions_by_name(self, name: str) -> list[DoMarsSession]:
        """List sessions whose name matches exactly.

        Used only to resolve an ambiguous create (timeout, 5xx-after-send, or
        409 live-name-exists): see ``do_mars_sandbox_service`` for the
        single-match adoption rule. Never used to invent a new create.
        """
        response = await self._request(
            'session.list', 'GET', '/v2/agents/sessions', params={'name': name}
        )
        if response.status_code >= 400:
            raise SandboxError('DigitalOcean MARS session list failed', status_code=502)
        items = response.json().get('sessions', [])
        return [self._as_session(item) for item in items if item.get('name') == name]

    async def pause_session(self, session_id: str) -> None:
        validate_session_id(session_id)
        response = await self._request(
            'session.pause', 'POST', f'/v2/agents/sessions/{session_id}/pause'
        )
        if response.status_code not in (200, 202, 204) and response.status_code != 409:
            raise SandboxError('DigitalOcean MARS pause failed', status_code=502)

    async def resume_session(self, session_id: str) -> None:
        validate_session_id(session_id)
        response = await self._request(
            'session.resume', 'POST', f'/v2/agents/sessions/{session_id}/resume'
        )
        if response.status_code not in (200, 202, 204) and response.status_code != 409:
            raise SandboxError('DigitalOcean MARS resume failed', status_code=502)

    async def delete_session(self, session_id: str) -> None:
        validate_session_id(session_id)
        response = await self._request(
            'session.delete', 'DELETE', f'/v2/agents/sessions/{session_id}'
        )
        if response.status_code not in (200, 202, 204, 404):
            raise SandboxError('DigitalOcean MARS delete failed', status_code=502)

    @staticmethod
    def port_forward_url(session_id: str, port: int) -> str:
        """The WS URL the gateway dials for raw-TCP port forwarding."""
        validate_session_id(session_id)
        if port not in (8000, 8011):
            raise ValueError('Only Agent Server and worker preview ports are supported')
        return (
            f'{DO_API_ORIGIN.replace("https://", "wss://", 1)}'
            f'/v2/agents/sessions/{session_id}/port-forward/{port}'
        )


def get_do_mars_client(
    team_token: str, httpx_client: httpx.AsyncClient
) -> DoMarsClient:
    """A fresh client per call.

    Unlike the AWS/Lambda client, this is not process-cached keyed by the
    token string, so a token rotation (D3) takes effect immediately rather
    than reusing a stale cached client. The client itself holds no
    connection-level state beyond the shared ``httpx_client``.
    """
    return DoMarsClient(team_token, httpx_client)
