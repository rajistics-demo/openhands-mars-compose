# Install (evaluation)

In this setup the Docker Compose host runs the Enterprise control plane: app, worker, automation-server, canvas, proxy, PostgreSQL and Redis. The new `mars-gateway` service also runs there. DigitalOcean MARS hosts the jobs, with one VM per sandbox. The scope is evaluation only. This package does **not** qualify production, model or capacity use (see STATUS.md).

## 0. Prerequisites

- **Licence and base images.** OpenHands Enterprise is licensed software (PolyForm Free Trial terms or your commercial agreement). You need registry access to `ghcr.io/openhands/enterprise-server:1.88.0`, plus a valid `OPENHANDS_LICENSE_KEY` if your agreement requires one. This package contains **only** the new MARS modules and narrow patches. It does not contain or redistribute the Enterprise product. Automation 1.30.0 is MIT.
- An existing **stock 1.88.0** Compose install. Its database must be at alembic revision `213`.
- Docker Engine with Compose **v2.24.4 or newer** (needed for `!override`).
- **DigitalOcean MARS (Harness Runtime)** access:
  - A **dedicated, custom-scoped team token**. It is read only by app, worker and gateway.
  - An operator-created environment config using the **builtin `openhands` harness**. This is the tested path. Custom harness and digest pinning are rejected on Private Beta (VERSIONS.md).
  - An egress allowlist set to the exact Enterprise callback hostname, with no wildcards. Expect the platform default hosts to be added to it.
  - The deferred-init key that the config expects.
- Two public HTTPS hostnames with TLS:
  - the app origin, which is also the callback target and must be reachable from DO egress;
  - a **separate** runtime origin for `mars-gateway`.

  Turn off query-string access logging on the TLS terminator for the runtime hostname. `compose/Caddyfile.example` shows the two upstreams for host-installed Caddy. It assumes the stock app proxy listens on host loopback port 9043 and the gateway on 8100; adjust to your stock configuration. Configure DNS and normal TLS on your own host using the [official Compose guide](https://docs.openhands.dev/enterprise/docker-compose). The example has no query-bearing access logs. It is an illustrative configuration, not a separately live-qualified TLS deployment.

## Environment config

Use `templates/environment.example.yaml` with the official Harness Runtime console/CLI/API available to your DO account. Submit its contents as `manifest_yaml` when creating `/v2/agents/configs`; record the returned config ID in `mars.env`. The rendered manifest contains a bootstrap secret: keep it outside git. `OH_SECRET_KEY` must match the `init_api_key` secret file. The tested size is two vCPU / 4 GiB per guest. Set `keep_warm: true` so Enterprise owns idle/pause decisions.

Allow the **exact** public callback/API hostname. If a script uses `--check-transport` to call its own public runtime URL, also allow that exact runtime hostname. Wildcards are unsupported; the platform adds default hosts. The live evaluation uses a runtime ingress that also forwards the required Enterprise API/callback routes, so one exact egress hostname serves both functions. The portable example keeps separate origins and requires operators to configure their TLS terminator accordingly. No VM inbound port is public.

## 1. Preflight (read-only)

```bash
cp compose/mars.env.example /etc/openhands-mars/mars.env   # outside any git checkout; edit
export ENTERPRISE_COMPOSE=/path/to/enterprise-1.88.0/docker-compose.yml
scripts/preflight.sh "$ENTERPRISE_COMPOSE" /etc/openhands-mars/mars.env
```

The preflight checks:

- this package against `SHA256SUMS`;
- the Compose version;
- the required variables, that every URL is HTTPS, and that the runtime and app hostnames differ;
- that the secret files exist and are not world-readable;
- that both base images are present and accept the patches (`mars_apply.py --check` runs inside each stock image with `--network none`);
- that the database is at `213`, or already at `do_mars_001`.

## 2. Back up

Take a PostgreSQL dump of the Enterprise database (`openhands`) **and** the Automation database (`automations`), and keep the original Compose file list. The package migration changes a backup's revision (see ROLLBACK.md, "Backups").

## 3. Secrets

Put three files in `MARS_SECRETS_DIR`:

| File | Contents | Readers |
|---|---|---|
| `do_team_token` | DO MARS team token | app, worker, mars-gateway |
| `session_signing_key` | Random key of at least 32 bytes for the per-session JWTs, for example `openssl rand -base64 48` | app, worker, mars-gateway |
| `init_api_key` | Deferred-init key matching the MARS config | app, worker |

Set permissions so the gateway user (uid 42420) can read them and others cannot, for example `chown root:42420` and `chmod 0640`. Never put these values in `mars.env`, in Compose files, in git or in a guest.

## 4. Gateway database role (recommended)

The gateway does a plain read of five columns and takes no row lock, so column `SELECT` is the only grant it needs. On PostgreSQL 17 this role could read those columns but was denied `session_api_key`, any `UPDATE` and `FOR SHARE` (verified in the PostgreSQL migration/role tests). Create the role **after** step 6, because the columns must exist first:

```sql
CREATE ROLE openhands_mars_gateway LOGIN PASSWORD '<from your secret store>';
GRANT CONNECT ON DATABASE openhands TO openhands_mars_gateway;
GRANT USAGE ON SCHEMA public TO openhands_mars_gateway;
GRANT SELECT (id, backend, provider_resource_id, session_api_key_hash, lifecycle_state)
  ON v1_remote_sandbox TO openhands_mars_gateway;
```

With this role, the plaintext and encrypted session-key columns cannot be read. If you skip this step, the gateway falls back to `DB_USER`.

## 5. Build the images

```bash
docker build -f docker/enterprise.Dockerfile  -t registry.example.invalid/openhands-enterprise-mars:1.88.0-mars1 .
docker build -f docker/automation.Dockerfile -t registry.example.invalid/openhands-automation-mars:1.30.0-mars1 .
```

Each build fails without writing anything if the base differs from the release:

- a base file hash differs;
- a hunk does not apply exactly;
- the migration head is not `213`;
- an overlay file already exists;
- the result differs from `PATCHED_SHA256`.

Record the resulting image IDs or digests, and set `MARS_APP_IMAGE` and `MARS_AUTOMATION_IMAGE` to them.

## 6. Migrate, then start

The stock Compose file sets `RUN_MIGRATIONS_ON_STARTUP=true`, so the app applies `do_mars_001` (213 → do_mars_001) when it starts. The migration is additive: two nullable columns plus one unique index.

```bash
docker compose -f "$ENTERPRISE_COMPOSE" -f compose/docker-compose.mars.yml --env-file /etc/openhands-mars/mars.env up -d
docker compose ... exec -T db psql -U openhands -d openhands -tAc 'select version_num from alembic_version'   # do_mars_001
```

The overlay makes these changes:

- It sets `OH_SANDBOX_KIND` and `OH_SANDBOX_SPEC_KIND` to the MARS injectors.
- It removes the Docker socket from app and worker.
- It empties `AUTOMATION_SANDBOX_INTERNAL_URL`, exports a public guest API origin, and sets `AUTOMATION_BASE_URL` to the public callback origin. Leaving the stock `http://proxy` callback base would strand completion callbacks inside the Compose network.
- It adds `mars-gateway`, published only on `127.0.0.1:${MARS_GATEWAY_HOST_PORT}`. Point your TLS terminator for the runtime hostname at it.

LLM and model settings are **not** changed. The MARS injector defaults to ten sandboxes per identity. This is admission policy, not a queue or autoscaler: exceeding the configured limit can pause that identity's oldest RUNNING sandbox. Existing conversations under the same identity compete with its automation runs. Keep evaluation work within the cap and confirm provider quotas before raising it.

Keep the native run-key TTL longer than the maximum job timeout plus startup/cleanup headroom. The default 3600-second key supports this 120-second fixture; longer jobs need an explicitly chosen timeout and TTL (the opt-in run key is capped at 24 hours).

## 7. Validate one native run (deterministic, no LLM)

```bash
export OPENHANDS_HOST=https://app.example.invalid
export OPENHANDS_ORG_ID='<evaluation-org-id>'
export OPENHANDS_EXPECTED_EMAIL='<evaluation-user-email>'
# Set OPENHANDS_API_KEY securely in this shell; do not put it in a command history.
python3 validation/run_native_validation.py --runs 1
# After the single run passes:
python3 validation/run_native_validation.py --runs 10
```

With `--runs 1`, the script runs one inactive automation and one native run on one MARS VM. Each run:

1. computes `sum(x*x for x in range(10000)) == 333283335000`;
2. touches 64 MiB of RAM, writes a file in `/workspace`, holds for 120 s, and verifies both;
3. sends the native completion callback with `task_outcome`.

It exits 0 only if the run is `COMPLETED` and `run_metadata.task_outcome.passed` is true, and retains the inactive automation and its history. The dispatcher cleans up its sandbox.

**Known UI gap (reported by the coordinator, not changed here):** the released Canvas 1.35.0 run display reads `run_metadata.finish_tool_response`, not `run_metadata.task_outcome`. A custom-script native run therefore shows "Successful" with "No task outcome reported" even though `task_outcome` is persisted. Inspect it with `GET /api/automation/v1/{automation_id}/runs` (field `run_metadata.task_outcome`) or a read-only query on the Automation database. The run history is retained; guest VM logs are gone once the VM is cleaned up. Use `--runs 5` or `--runs 10` for independent standard dispatches on one inactive definition. The driver observes native RUNNING counts and validates outcomes; verify provider states/processes separately for a stronger concurrency claim. Its output contains private run IDs and must be saved outside git.
