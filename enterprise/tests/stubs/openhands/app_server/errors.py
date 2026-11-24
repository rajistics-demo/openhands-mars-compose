"""Local-only shim, NOT part of the overlay/patch applied to Enterprise.

do_mars_client.py and do_mars_tunnel.py import the real
``openhands.app_server.errors.SandboxError`` from the licensed Enterprise
source tree, which this standalone validation checkout does not have. This
stub exists solely so those two modules - and anything that imports them,
like do_mars_pool_provider.py - can be imported for local pytest runs
against this repo's own tests. It is added to ``sys.path`` only by
``enterprise/tests/conftest.py``, after the real overlay path, so it is
never consulted when the real module is present (as it always is once this
overlay is actually applied to an Enterprise checkout).
"""


class SandboxError(Exception):
    pass
