"""Focused regression tests for DoMarsPoolProviderAdapter's wiring onto the
real tunnel_http_request signature.

These exist because do_mars_pool_reconciler's own unit tests only ever
exercise PoolProvider through a fully synthetic FakePoolProvider (see
test_do_mars_pool_reconciler.py) - by design, so the reconciler's own logic
can be tested without any network/DO dependency. That design means a
keyword-argument mismatch between DoMarsPoolProviderAdapter and the real
tunnel_http_request it wraps is invisible to every test in that file: it
only ever shows up against the real function's signature, which these
tests check directly by monkeypatching tunnel_http_request itself (never
the adapter) and asserting on what the adapter actually passes it.
"""

from __future__ import annotations

import inspect

import pytest

from openhands.app_server.sandbox import do_mars_pool_provider
from openhands.app_server.sandbox.do_mars_client import SESSION_STATUS_PAUSED, DoMarsSession
from openhands.app_server.sandbox.do_mars_pool_provider import DoMarsPoolProviderAdapter
from openhands.app_server.sandbox.do_mars_tunnel import TunnelHttpResponse, tunnel_http_request

pytestmark = pytest.mark.asyncio


class _FakeDoMarsClient:
    def __init__(self):
        self.get_session_result: DoMarsSession | None = None

    def port_forward_url(self, session_id: str, port: int) -> str:
        return f'wss://tunnel.example/{session_id}:{port}'

    async def get_session(self, session_id: str):
        return self.get_session_result


async def test_probe_dormant_calls_tunnel_http_request_with_its_real_keyword(monkeypatch):
    """DoMarsPoolProviderAdapter.probe_dormant must call the real
    tunnel_http_request using *its* parameter name for the bearer token,
    not the adapter's own field name (do_team_token) - these are not
    required to match, and on a real deployment they did not: every call
    raised TypeError('tunnel_http_request() got an unexpected keyword
    argument...'), swallowed by the reconciler's own try/except as a plain
    "not dormant yet" result (see do_mars_pool_reconciler._create_one and
    _reconcile_booting), making a guest that was actually already
    dormant-ready look stuck booting forever.

    This binds the call against ``inspect.signature`` of the *real*
    tunnel_http_request (imported, never redefined here) rather than
    hardcoding the expected keyword name as a string literal in this test,
    so a future rename of either side's parameter is still caught: the
    only way this test can pass is if the adapter's call and the real
    function's signature actually agree.
    """

    captured_kwargs: dict = {}

    async def fake_tunnel_http_request(url, **kwargs):
        captured_kwargs['url'] = url
        captured_kwargs.update(kwargs)
        return TunnelHttpResponse(status_code=200, headers={}, body=b'{"state": "dormant"}')

    monkeypatch.setattr(do_mars_pool_provider, 'tunnel_http_request', fake_tunnel_http_request)

    adapter = DoMarsPoolProviderAdapter(do=_FakeDoMarsClient(), do_team_token='token-xyz')
    result = await adapter.probe_dormant('session-1')

    assert result is True
    real_params = set(inspect.signature(tunnel_http_request).parameters)
    bearer_params = {k for k in captured_kwargs if k.endswith('_token') and k != 'url'}
    assert bearer_params, f'adapter passed no token-shaped keyword at all: {captured_kwargs}'
    for name in bearer_params:
        assert name in real_params, (
            f'adapter passed keyword {name!r}, which tunnel_http_request does not '
            f'accept (its real parameters are {sorted(real_params)}) - this is '
            'exactly the mismatch that silently broke every dormant probe'
        )
    assert captured_kwargs[next(iter(bearer_params))] == 'token-xyz'


async def test_probe_dormant_reports_not_ready_on_non_dormant_body(monkeypatch):
    async def fake_tunnel_http_request(url, **kwargs):
        return TunnelHttpResponse(status_code=200, headers={}, body=b'{"state": "running"}')

    monkeypatch.setattr(do_mars_pool_provider, 'tunnel_http_request', fake_tunnel_http_request)
    adapter = DoMarsPoolProviderAdapter(do=_FakeDoMarsClient(), do_team_token='token-xyz')
    assert await adapter.probe_dormant('session-1') is False


async def test_is_still_paused_reads_provider_status_only(monkeypatch):
    """is_still_paused must never go anywhere near tunnel_http_request -
    it is a control-plane status read, the one permitted health signal for
    paused inventory (see PoolProvider.is_still_paused's own docstring)."""

    async def fail_if_called(*args, **kwargs):
        raise AssertionError('is_still_paused must never dial the tunnel')

    monkeypatch.setattr(do_mars_pool_provider, 'tunnel_http_request', fail_if_called)
    client = _FakeDoMarsClient()
    client.get_session_result = DoMarsSession(
        id='session-1',
        name='session-1',
        status=SESSION_STATUS_PAUSED,
        config_id=None,
        created_at=None,
    )
    adapter = DoMarsPoolProviderAdapter(do=client, do_team_token='token-xyz')
    assert await adapter.is_still_paused('session-1') is True
