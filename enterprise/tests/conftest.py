"""Test-collection path setup only - never applied to the Enterprise tree.

Appends enterprise/tests/stubs to sys.path, *after* whatever path already
put enterprise/overlay on it (normally via PYTHONPATH when running this
repo's tests standalone). Both trees are plain PEP 420 namespace packages
(no __init__.py), so Python merges them: a real overlay module always wins
import resolution for its own dotted path, and the stub is only ever
reached for a module the overlay does not provide, such as
openhands.app_server.errors - see stubs/openhands/app_server/errors.py for
why that one specific module needs a stand-in here.
"""

import sys
from pathlib import Path

_STUBS_DIR = str(Path(__file__).resolve().parent / 'stubs')
if _STUBS_DIR not in sys.path:
    sys.path.append(_STUBS_DIR)
