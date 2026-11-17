# Rollback, downgrade and backups

## Compatibility facts (tested on PostgreSQL 17)

- `do_mars_001` revises `213`. It adds the nullable columns `provider_resource_id` and `provider_create_intent_at` and the unique index `ix_sandbox_backend_provider_resource`. Rows from other backends are not touched.
- A **stock** 1.88.0 image pointed at a database stamped `do_mars_001` fails at `alembic upgrade head` with `Can't locate revision identified by 'do_mars_001'`. With `RUN_MIGRATIONS_ON_STARTUP=true` the stock app will not start. **Always move the stamp back before you run a stock image.**
- The same rule holds for **backups**. A dump taken after install carries `do_mars_001`. To restore it into a stock image, first restore it into a scratch database and then apply option A or B below. A pre-install dump (step 2 of INSTALL.md) restores into stock 1.88.0 directly.
- Upgrading to a later stock Enterprise release (which ships its own `214`+) also requires moving the stamp back to `213` first. The package id is deliberately non-numeric so that it can never be mistaken for upstream `214`.

## Procedure

1. **Drain MARS work:**
   - Disable the MARS automations.
   - Let runs finish, or cancel them.
   - Pause or delete the MARS sandboxes from Enterprise, so that Enterprise deletes the DO sessions.
   - Then list the remaining DO sessions and saved configs **created for this evaluation** and delete them. Delete only resources you can match to your own records.
2. **Resolve unresolved intents.** These are rows with `backend='do-mars'`, `provider_create_intent_at IS NOT NULL` and `provider_resource_id IS NULL`. Enterprise never clears them by elapsed time. For each row, look up DO sessions by the exact row name. If no session exists, or you have deleted the matching session yourself, delete the row by hand and record what you did. Do not infer ownership from DO.
3. **Schema.** Choose one option, and run it with the **MARS image** (which has the migration file):
   - **A. Remove the columns:** `alembic downgrade 213`. The MARS mapping is lost, so finish steps 1 and 2 first.
   - **B. Keep the columns (fast):** `alembic stamp 213`. Stock code ignores the extra nullable columns. A later re-install is safe, because the DDL is `IF NOT EXISTS`.
4. **Restore Compose.** Re-apply the original Compose file list and set `APP_IMAGE` / `AUTOMATION_IMAGE` back to stock (or unset them). Recreate `app`, `worker` and `automation-server`, and remove `mars-gateway`.
5. **Verify.** `alembic_version` is `213`. `/alive` is healthy. A stock Docker-sandbox conversation starts.

The Automation patches add **no** migration. `task_outcome` lives in the existing `run_metadata` JSON, and expiring run keys use the existing `expires_at` column. Rolling the image back is enough. Keys issued while `AUTOMATION_RUN_KEY_TTL_SECONDS` was set expire on their own.

## Unresolved intents during normal operation

A delete attempt on an unresolved row raises `MARS unresolved intent requires operator reconciliation`, and the lifecycle sweep retries it on each run. Reconcile the row with step 2. Retrying more often does not help.
