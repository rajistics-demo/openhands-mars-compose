"""Add DO MARS create-intent / provider-id mapping columns to v1_remote_sandbox.

Revision ID: do_mars_doks_001
Revises: 221

Package-owned, not an upstream Enterprise revision. This is the DOKS/Helm
package's own migration line, separate from the 1.88.0 Compose package's
``do_mars_001``/``do_mars_002`` (down_revision ``213``) - see
``doks/VERSIONS.md`` for why the two lines cannot be merged. It is rebased
onto the Enterprise 1.97.1 release head (``221``) because this package
targets a fresh or stock 1.97.1 Helm installation, not an upgrade path from
the 1.88.0 Compose database. A database that is already on the 1.88.0
package's migration line is a different, unsupported install for this
package: ``mars_apply_doks.py``'s head check refuses to proceed rather than
silently re-stamping or rewriting it.

Uses a non-numeric id on purpose: upstream later releases ship their own
numbered revisions, and reusing a number would let a later stock image
believe its own migration had already run.

Additive and nullable: every existing row of every backend gets NULL in both
columns. ``IF NOT EXISTS`` keeps the upgrade safe on a database that kept the
columns after a ``stamp 221`` rollback (see the package ROLLBACK guide).
"""

from typing import Sequence

from alembic import op

revision: str = 'do_mars_doks_001'
down_revision: str | None = '221'
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
