"""Privacy-bounded diagnostic logging for the DO MARS provider and gateway.

Every field is validated against a closed vocabulary or a strict shape, so a
record can carry an HTTP method, a status code, an exception class name or a
provider status enum, but never a URL, header, token, body, exception text or
sandbox/session identifier. Anything that does not validate is dropped.
"""

import logging
import re

_logger = logging.getLogger('openhands.app_server.sandbox.do_mars_diagnostics')

EVENTS = frozenset(
    {
        'do_mars.control_response',
        'do_mars.control_transport_error',
        'do_mars.delete_unconfirmed',
        'do_mars.cleanup_after_init_failure_failed',
        'do_mars.init_failed',
        'do_mars.idle_probe_failed',
        'do_mars.health_probe_failed',
        'do_mars.tunnel_ws_failed',
        'do_mars.tunnel_ws_closed',
        'do_mars.tunnel_request_failed',
        'do_mars.gateway_guest_response',
        'do_mars.gateway_guest_transport_error',
        'do_mars.gateway_guest_ws_failed',
    }
)
OPERATIONS = frozenset(
    {
        'session.create',
        'session.get',
        'session.list',
        'session.pause',
        'session.resume',
        'session.delete',
        'init.probe',
        'init.post',
        'gateway.http',
        'gateway.ws',
        'idle.probe',
        'health.probe',
    }
)
PHASES = frozenset({'handshake', 'stream', 'provider_frame', 'request', 'response'})
REASONS = frozenset(
    {
        'control_error',
        'confirmation_timeout',
        'unresolved_intent',
        'not_dormant',
        'bad_status',
        'transport_error',
    }
)
METHODS = frozenset({'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'})
FRAME_TYPES = frozenset({'TEXT', 'ERROR', 'CLOSE', 'CLOSED', 'CLOSING'})
_CLASS_NAME = re.compile(r'\A[A-Z][A-Za-z0-9_]{0,63}\Z')
_PROVIDER_STATUS = re.compile(r'\ASESSION_STATUS_[A-Z_]{1,40}\Z')
_INT_FIELDS = frozenset({'status_code', 'handshake_status', 'close_code', 'attempts'})


def _valid(name: str, value: object) -> object | None:
    if isinstance(value, bool):
        return None
    if name in _INT_FIELDS:
        return value if isinstance(value, int) and 0 <= value < 100000 else None
    if not isinstance(value, str):
        return None
    if name == 'error_class':
        return value if _CLASS_NAME.fullmatch(value) else None
    if name == 'provider_status':
        return value if _PROVIDER_STATUS.fullmatch(value) else 'unrecognized'
    vocabulary = {
        'operation': OPERATIONS,
        'phase': PHASES,
        'reason': REASONS,
        'method': METHODS,
        'frame_type': FRAME_TYPES,
    }.get(name)
    return value if vocabulary is not None and value in vocabulary else None


def error_class(exc: BaseException | None) -> str | None:
    return type(exc).__name__ if exc is not None else None


def log_diagnostic(event: str, *, level: int = logging.WARNING, **fields) -> None:
    """Log ``event`` with only the validated ``fields``; never exception text."""
    if event not in EVENTS:
        raise ValueError('unknown MARS diagnostic event')
    extra = {
        f'mars_{name}': checked
        for name, value in fields.items()
        if (checked := _valid(name, value)) is not None
    }
    _logger.log(level, event, extra=extra)
