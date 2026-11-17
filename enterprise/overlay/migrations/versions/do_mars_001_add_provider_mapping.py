"""Add DO MARS create-intent / provider-id mapping columns to v1_remote_sandbox.

Revision ID: do_mars_001
Revises: 213

Package-owned, not an upstream Enterprise revision. It is rebased onto the
Enterprise 1.88.0 release head (213) and uses a non-numeric id on purpose:
upstream later releases ship their own ``214``, and reusing that number would
let a later stock image believe its own 214 had already run.

Additive and nullable: every existing row of every backend gets NULL in both
columns. ``IF NOT EXISTS`` keeps the upgrade safe on a database that kept the
columns after a ``stamp 213`` rollback (see the package ROLLBACK guide).
"""

from typing import Sequence

from alembic import op

revision: str = 'do_mars_001'
down_revision: str | None = '213'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        'ALTER TABLE v1_remote_sandbox '
        'ADD COLUMN IF NOT EXISTS provider_resource_id VARCHAR NULL'
    )
    op.execute(
        'ALTER TABLE v1_remote_sandbox '
        'ADD COLUMN IF NOT EXISTS provider_create_intent_at '
        'TIMESTAMP WITH TIME ZONE NULL'
    )
    op.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS ix_sandbox_backend_provider_resource '
        'ON v1_remote_sandbox (backend, provider_resource_id)'
    )


def downgrade() -> None:
    op.execute('DROP INDEX IF EXISTS ix_sandbox_backend_provider_resource')
    op.execute(
        'ALTER TABLE v1_remote_sandbox DROP COLUMN IF EXISTS provider_create_intent_at'
    )
    op.execute(
        'ALTER TABLE v1_remote_sandbox DROP COLUMN IF EXISTS provider_resource_id'
    )
