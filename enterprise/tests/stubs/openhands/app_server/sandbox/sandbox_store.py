"""Local-only shim, NOT part of the overlay/patch applied to Enterprise.

do_mars_proxy.py imports ``LifecycleState``, ``StoredSandbox`` and
``hash_session_api_key`` from the real Enterprise ``sandbox_store`` module,
which this standalone validation checkout does not have. See
``enterprise/tests/stubs/openhands/app_server/errors.py`` for why a stub is
needed at all and how it is wired onto ``sys.path`` only for local pytest
runs (``enterprise/tests/conftest.py``), never for the real overlay.

Scope, deliberately minimal and checked against actual usage in
do_mars_proxy.py and its test before writing this:

- ``StoredSandbox`` is referenced only inside ``do_mars_proxy._load_row``,
  to build a SQLAlchemy ``select()`` against the real sandbox table. Every
  existing test monkeypatches ``_load_row`` itself, so that function body -
  and this class's actual column shape - is never executed; it only needs
  to exist as an importable name with the three attributes referenced
  there, as plain class attributes (not real mapped columns - no test
  constructs a working SQL query against this stub).
- ``LifecycleState`` IS exercised (``row.lifecycle_state != LifecycleState.
  RUNNING``), compared against plain strings like ``'running'``/``'paused'``
  the tests assign directly - so, like this codebase's own
  ``do_mars_pool_models.PoolSlotState``, it must be a ``StrEnum`` whose
  member values are exactly those strings for that comparison to mean
  anything.
- ``hash_session_api_key`` IS exercised on both sides of one check
  (``do_mars_proxy.authorize`` and the test's own row setup both call it),
  so the test only needs it to be a deterministic function of the key,
  identical on both call sites because both import the same module - never
  the real Enterprise hashing scheme, which this stub deliberately does not
  attempt to reproduce.
"""

import hashlib
from dataclasses import dataclass
from enum import StrEnum


class LifecycleState(StrEnum):
    RUNNING = 'running'
    PAUSED = 'paused'
    PAUSING = 'pausing'
    RESUMING = 'resuming'
    STOPPED = 'stopped'


@dataclass
class StoredSandbox:
    id: str
    backend: str
    provider_resource_id: str | None
    session_api_key_hash: str | None
    lifecycle_state: str


def hash_session_api_key(key: str) -> str:
    return hashlib.sha256(f'stub-sandbox-store:{key}'.encode()).hexdigest()
