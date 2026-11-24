"""Add the opt-in DO MARS warm-pool inventory table.

Revision ID: do_mars_doks_002
Revises: do_mars_doks_001

Package-owned, not an upstream Enterprise revision, same as do_mars_doks_001.
This is the DOKS/Helm package's own migration line (rebased onto 1.97.1 head
``221``), separate from the 1.88.0 Compose package's ``do_mars_002``.
Forward-only from do_mars_doks_001: this never edits that revision's columns or
index, it only adds a new, independent table. A site that never enables the
pool (``DO_MARS_POOL_ENABLED`` unset or ``false``) gets an empty, unused
table and no behavior change; nothing here is read unless the pool injector
is configured.

``do_mars_pool_slot`` is deliberately **not** a subtype or extension of
``v1_remote_sandbox``. Pool inventory has no tenant owner until claimed, and
every existing ``v1_remote_sandbox`` row, query, and constraint (owner
columns, lifecycle state, pause semantics) is NOT NULL / owner-scoped by
contract; adding owner-less rows there would either violate that contract or
require loosening it for every other backend. Keeping the two tables
separate means claiming a slot is "insert into v1_remote_sandbox, delete
from do_mars_pool_slot" in one transaction (see do_mars_pool_store.py) and
every non-pool code path is completely unaffected.

Additive and nullable where it matters: ``IF NOT EXISTS`` throughout so a
re-run (or a database that already has this from a prior partial upgrade)
is a no-op rather than an error.
"""

from typing import Sequence

from alembic import op

revision: str = 'do_mars_doks_002'
down_revision: str | None = 'do_mars_doks_001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS do_mars_pool_slot (
            id VARCHAR PRIMARY KEY,
            backend VARCHAR NOT NULL,
            sandbox_spec_id VARCHAR NOT NULL,
            config_id VARCHAR NOT NULL,
            state VARCHAR NOT NULL,
            provider_resource_id VARCHAR NULL,
            provider_create_intent_at TIMESTAMP WITH TIME ZONE NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            state_changed_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            last_health_check_at TIMESTAMP WITH TIME ZONE NULL,
            last_health_ok BOOLEAN NULL,
            claim_owner_sandbox_id VARCHAR NULL,
            claimed_by_user_id VARCHAR NULL,
            claimed_org_id VARCHAR NULL,
            error_detail VARCHAR NULL
        )
        """
    )
    # One row of history per slot id, never reused: a slot that errors or is
    # drained is deleted, not recycled, so a unique primary key is enough to
    # keep "never hand the same provider VM to two tenants" true even if a
    # row were ever reinserted with the same id (it never is in normal
    # operation; this is a belt-and-braces constraint, not a hot path).
    op.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS ix_do_mars_pool_slot_provider_resource '
        'ON do_mars_pool_slot (backend, provider_resource_id) '
        'WHERE provider_resource_id IS NOT NULL'
    )
    # The replenish/claim/health scans all filter on (backend, spec, state);
    # this index keeps those bounded scans index-only even with a large
    # claimed/error history before it is reaped.
    op.execute(
        'CREATE INDEX IF NOT EXISTS ix_do_mars_pool_slot_lookup '
        'ON do_mars_pool_slot (backend, sandbox_spec_id, config_id, state)'
    )


def downgrade() -> None:
    op.execute('DROP INDEX IF EXISTS ix_do_mars_pool_slot_lookup')
    op.execute('DROP INDEX IF EXISTS ix_do_mars_pool_slot_provider_resource')
    op.execute('DROP TABLE IF EXISTS do_mars_pool_slot')
