"""The catalog identifies a DO MARS Environment Config without exposing it.

Each ``DoMarsSpecInfo.id`` maps to exactly one operator-created DO
Environment Config (``config_id``). Per-start config creation is forbidden
(4.11 of the design): creating a config per session would auto-save one that
survives session removal and must then be tracked and cleaned up separately.
Config/template creation itself is out of scope for the app; see
``scripts/do_mars/``.
"""

import os
from typing import AsyncGenerator

from fastapi import Request
from pydantic import Field, SecretStr

from openhands.app_server.sandbox.do_mars_secrets import read_secret
from openhands.app_server.sandbox.preset_sandbox_spec_service import (
    PresetSandboxSpecService,
)
from openhands.app_server.sandbox.sandbox_spec_models import SandboxSpecInfo
from openhands.app_server.sandbox.sandbox_spec_service import (
    SandboxSpecService,
    SandboxSpecServiceInjector,
)
from openhands.app_server.services.injector import InjectorState


class DoMarsSpecInfo(SandboxSpecInfo):
    """An operator-owned DO MARS template binding.

    ``config_id`` and ``init_api_key`` are excluded from API output: a spec
    read must never leak either the DO config id or the deferred-init
    bootstrap secret to a client.
    """

    config_id: str = Field(exclude=True)
    init_api_key: SecretStr = Field(exclude=True)


class DoMarsSpecServiceInjector(SandboxSpecServiceInjector):
    specs: list[DoMarsSpecInfo] = Field(
        default_factory=lambda: [
            DoMarsSpecInfo(
                id='do-mars-agent-server',
                command=None,
                working_dir='/workspace',
                config_id=os.environ['DO_MARS_CONFIG_ID'],
                init_api_key=SecretStr(read_secret('DO_MARS_INIT_API_KEY')),
            )
        ]
    )

    async def inject(
        self, state: InjectorState, request: Request | None = None
    ) -> AsyncGenerator[SandboxSpecService, None]:
        yield PresetSandboxSpecService(specs=list(self.specs))
