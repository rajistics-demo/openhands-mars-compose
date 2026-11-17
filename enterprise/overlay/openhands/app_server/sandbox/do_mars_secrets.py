"""Server-only secret references, supporting read-only Compose secret mounts."""

import os
from pathlib import Path


def read_secret(name: str) -> str:
    filename = os.getenv(f'{name}_FILE')
    value = Path(filename).read_text().strip() if filename else os.environ[name]
    if not value:
        raise ValueError(f'{name} is empty')
    return value
