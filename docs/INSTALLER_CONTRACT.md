# Installer contract (evaluation)

This document gives the exact commands of the staged setup tool `installer/mars_setup.py` and the release builder `installer/mars_release.py`, and the limits of what was tested. Both tools use only the Python standard library. Setup and procedures stay in INSTALL.md and ROLLBACK.md; this document does not replace them.

## Requirements and non-goals

- Linux host, Python 3.11 or newer, conventional (rootful) Docker Engine with Compose **2.24.4 or newer**, and a user that can run `docker` without `sudo` (the `docker` group). Rootless Docker, user-namespace remapping and Docker Desktop are not qualified: the uid/gid 42420 permission model assumes container ids equal host ids.
- The tool never installs Docker, Caddy or system packages. It never runs `sudo` or changes ownership, and never opens firewall ports or edits DNS. It makes no MARS or DigitalOcean API calls and does not create application users, organizations, API keys, schedules or automations. It never runs `down`, `down -v`, `alembic stamp` or a downgrade.
- Env files are parsed as plain `KEY=value` data with a restricted character set. They are never sourced or executed. Values with `$`, quotes, backticks, spaces or `#` are refused. Docker is called with an allowlisted environment (`PATH`, `HOME`, `DOCKER_*` and similar), so shell variables such as `DB_PASS` or `COMPOSE_FILE` cannot change the merge.
- Secret values are never accepted as flags, printed, or included in error messages. `compose config` output is parsed in memory and never printed. `docker compose up` output is printed with known secret values replaced by `<redacted>`.

## Pins (`installer/release.lock.json`)

| Item | Value |
|---|---|
| Package version | `1.88.0-mars2` |
| Enterprise | 1.88.0, commit `c6191be5fb66e5ed6472062d9f6a554b58570d97` |
| Stock Compose | `https://raw.githubusercontent.com/OpenHands/enterprise/c6191be5fb66e5ed6472062d9f6a554b58570d97/docker-compose.yml`, 9539 bytes, SHA256 `d17ec0f72978b50f0d1df666e9ad8c2e7cdd9b2938601b52b3a4dcfb97d7ac55` |
| Stock images (baseline) | `APP_IMAGE` / `AUTOMATION_IMAGE` set to the digests in `docker/*.Dockerfile` |
| Local MARS images | `openhands-enterprise-mars:1.88.0-mars2`, `openhands-automation-mars:1.30.0-mars2` (no registry; never pushed) |
| Migrations | base `213`, package `do_mars_001` |
| Sandbox cap | 10 per identity (not configurable here) |

The same values are hard-coded in `mars_setup.py`, and every command refuses a lock file that disagrees with them. The Canvas image (`agent-canvas:1.35.0`), `postgres:16`, `redis:7` and `caddy:2` keep the stock file's tags and are not pinned by digest.

## Install directory

`init` takes a separate directory outside the package. It refuses a non-empty directory that it did not create, and a path inside the package or one that contains it.

```
<dir>/state.json                     project, directory, package version, ownership intent, build IDs (0600)
<dir>/site.env                       your non-secret site settings
<dir>/private/stack.env              generated DB/service/webhook/KV secrets + optional licence key (0600)
<dir>/private/compose.env            derived from site.env by the tool; edits are detected and refused
<dir>/private/environment.yaml       render-env-spec output; contains the bootstrap secret (0600)
<dir>/compose/docker-compose.yml     the pinned stock file (fetch-compose)
<dir>/compose/docker-compose.site.yml   loopback proxy + public CORS overlay (copied from installer/compose)
<dir>/compose/docker-compose.mars.yml   the MARS overlay (copied from compose/)
<dir>/secrets/do_team_token          empty: put your DO token here with your editor
<dir>/secrets/session_signing_key    generated (64 characters)
<dir>/secrets/init_api_key           generated; must match OH_SECRET_KEY in your MARS config
```

Re-running `init` with the same arguments keeps every existing file, including secrets, byte for byte. Different origins, ports or project name are refused. Later commands take `--dir` only. They refuse a copied or moved directory (`state.json` records the absolute path) and an install created by another package version.

`site.env` keys: `APP_ORIGIN`, `RUNTIME_ORIGIN` (different public `https://host` origins, no port or path), `DO_MARS_CONFIG_ID` (UUID; may be empty until `--stage mars`), `APP_PORT`, `MARS_GATEWAY_HOST_PORT`. The optional keys are `ENABLE_POSTHOG_ANALYTICS`, `POSTHOG_CLIENT_KEY`, `DO_MARS_SESSION_TTL_SECONDS`, `AUTOMATION_RUN_KEY_TTL_SECONDS` and `OH_SANDBOX_LIFECYCLE_IDLE_SECONDS`. Unknown keys are refused.

## Commands

Run all commands from the extracted package directory. `PKG` is that directory and `DIR` is your install directory.

```bash
# 1. Local preparation only. Starts nothing; checks for an existing Compose project/volumes if Docker is reachable.
python3 installer/mars_setup.py init --dir "$DIR" --project oh-mars-eval \
    --app-origin https://app.your-domain.example --runtime-origin https://runtime.your-domain.example

# 2. Fetch ONLY the pinned stock Compose file (HTTPS to raw.githubusercontent.com, no redirects,
#    64 KiB / 30 s limits, hash verified before an atomic write). Offline: add --from-file <copy>.
python3 installer/mars_setup.py fetch-compose --dir "$DIR"

# 3. Baseline readiness (read-only apart from private/compose.env).
python3 installer/mars_setup.py check --dir "$DIR" --stage baseline

# 4. Explicitly start the STOCK baseline (stock images by digest, Docker sandboxes, no MARS).
python3 installer/mars_setup.py up --dir "$DIR" --baseline
python3 installer/mars_setup.py status --dir "$DIR"
```

Then, yourself and outside the tool:

1. Point your TLS terminator for `APP_ORIGIN` at `127.0.0.1:APP_PORT`.
2. Sign in and configure an LLM.
3. **Complete one normal conversation.**
4. Back up the `openhands` and `automations` databases (INSTALL.md step 3).

```bash
# 5. Build the two local images from the pinned stock digests; records their image IDs in state.json.
python3 installer/mars_setup.py build --dir "$DIR"

# 6. Render the MARS environment spec locally (0600; contents never printed). Create the MARS config
#    yourself with the official DigitalOcean console/CLI/API, then set DO_MARS_CONFIG_ID in site.env.
python3 installer/mars_setup.py render-env-spec --dir "$DIR"
"$EDITOR" "$DIR/secrets/do_team_token"

# 7. Gateway read permissions (manual; the tool only checks). mars-gateway runs as uid/gid 42420.
#    Keep the directory owned by YOU (the installer user); change only its group.
sudo chgrp 42420 "$DIR/secrets" && chmod 0750 "$DIR/secrets"
sudo chown root:42420 "$DIR/secrets/do_team_token" "$DIR/secrets/session_signing_key"
sudo chmod 0640 "$DIR/secrets/do_team_token" "$DIR/secrets/session_signing_key"
# init_api_key stays yours with mode 0600 (read only by app/worker, which run as root).

# 8. Activation readiness, then explicit activation with your two attestations.
python3 installer/mars_setup.py check --dir "$DIR" --stage mars
python3 installer/mars_setup.py up --dir "$DIR" --mars --baseline-verified --backup-confirmed
python3 installer/mars_setup.py status --dir "$DIR"     # expect alembic_version do_mars_001
```

After activation, point your TLS terminator for `RUNTIME_ORIGIN` at `127.0.0.1:MARS_GATEWAY_HOST_PORT` and follow INSTALL.md step 6. `compose-command --dir "$DIR" --stage baseline|mars` prints the exact `docker compose -p … -f … --env-file …` prefix for your own `ps` and `logs`. It prints paths only. Never paste or publish `config` output, because it contains secrets.

## Readiness contract

`check` prints one `ok`, `warn` or `FAIL` line per check and exits 0 only when nothing failed.

`READY baseline` means:

- the lock and Dockerfile pins match, and the package files match `SHA256SUMS`;
- the stock file hash matches;
- the env files are valid and private;
- the merged model (real `docker compose config`) binds every published port to `127.0.0.1`;
- `OH_WEB_URL`, `WEB_HOST`, `PERMITTED_CORS_ORIGINS`, `AUTOMATION_CORS_ORIGINS` and Canvas `--lock-to-cloud` are the public app origin, and `AUTOMATION_OPENHANDS_API_BASE_URL` is `http://app:3000`;
- every stock default secret was replaced by a generated one;
- the Compose project is unused or owned by this install.

`READY mars-activation` adds the following:

- `DO_MARS_CONFIG_ID` is set;
- the merged model has the public native callback (`AUTOMATION_BASE_URL`) and the public guest API, an empty `AUTOMATION_SANDBOX_INTERNAL_URL`, cap 10, no Docker socket on app/worker and the local MARS images;
- the secrets directory is still owned by the installer user and traversable by gid 42420, and the secret files are non-empty, not world-accessible and (token and signing key) readable by uid/gid 42420. `check` only calls `stat()`. If the directory was made `root:42420 0750`, the installer user cannot stat its children, and `check` reports a FAIL line instead of guessing;
- both local tags resolve to the image IDs that `build` recorded for this install;
- every baseline service is running, and services with a healthcheck are healthy;
- `alembic_version` is exactly `213` or `do_mars_001`.

Any other revision is refused, and the tool will not stamp, downgrade or overwrite it.

A READY result means the configuration and local state are consistent. **It does not prove an end-to-end install.** You confirm the baseline conversation yourself, and the `--baseline-verified` flag records that confirmation. The tool cannot check it.

## Ownership, retries and the way back

- `up` records an ownership intent (project and exact Compose file paths) in `state.json` **before** calling Docker. If `up` fails after Compose has created volumes, re-running `up` for the same project and the same files is allowed. Volumes or a Compose project created by anyone else are still refused.
- After `up --mars` has been attempted, `up --baseline` is refused, because stock 1.88.0 cannot start on `do_mars_001`. Before any later restart of the baseline, the tool also requires `alembic_version` to be `213`. To return to stock, follow ROLLBACK.md by hand.
- Re-running `up --mars` is supported while the database is at `213` or `do_mars_001`.

## Gateway database role

On first activation, `mars-gateway` uses the shared application role (`DB_USER`, generated `DB_PASS`), and `check` prints a `warn` line saying so. The least-privilege role cannot be created before `do_mars_001` adds its columns. The manual follow-up after activation is:

1. Create the role with the SQL in INSTALL_MANUAL.md step 4.
2. Add `MARS_GATEWAY_DB_USER` and `MARS_GATEWAY_DB_PASS` to `private/stack.env`.
3. Run `check --stage mars`. The tool verifies that the role has column `SELECT` on `session_api_key_hash`, cannot read `session_api_key` and cannot `UPDATE`.
4. Re-run `up --mars`.

Setting the role while the database is still at `213` is refused.

## Release builder

```bash
python3 installer/mars_release.py sums --check            # source SHA256SUMS == manifest files
python3 installer/mars_release.py build --out /tmp/mars-release            # git checkout: HEAD recorded, verified
python3 installer/mars_release.py build --out /tmp/mars-release --source-commit <40-hex>   # no git: declared
python3 installer/mars_release.py verify /tmp/mars-release/openhands-enterprise-mars-1.88.0-mars2.zip
```

How the builder selects and writes files:

- Only paths in `installer/release-files.txt` are shipped. The list must be sorted, and each entry carries a `0644` or `0755` mode.
- The builder refuses symlinks, `..`, absolute paths, caches, `.git`, env files, `secrets/` and `private/` paths, logs and archives. It fails if a listed file is missing.
- In a git checkout, every listed file must be tracked and unmodified, with the listed mode.
- `--out` must be outside the source tree. Existing archives or checksum sidecars with different content are never overwritten, and symlink outputs are refused.
- The ZIP has one root, `openhands-enterprise-mars-<version>/`, stored entries, a fixed 1980-01-01 timestamp and Unix modes.
- The ZIP adds `RELEASE.json` (version, Enterprise commit, stock Compose hash, source commit and whether it was verified) and a `SHA256SUMS` that covers exactly every other file. The builder also writes `<zip>.sha256` next to the ZIP.
- Repeated builds from the same files and the same provenance mode give a byte-identical ZIP. A no-git rebuild records declared provenance and therefore differs from a git-verified build.

**The checksums are unsigned.** They detect corruption, not tampering, and they make no authenticity claim.

## Tests and tested limits

```bash
MARS_STOCK_COMPOSE=/path/to/stock-1.88.0/docker-compose.yml python3 -m unittest discover -s installer/tests -v
```

The tests use a fake `docker` CLI (`installer/tests/fake_docker.py`) for daemon calls such as `ls`, `ps`, `exec`, `up`, `build` and `image inspect`. They forward `compose config` to the real Docker CLI. Without `MARS_STOCK_COMPOSE`, the Compose-merge and downloader tests are skipped.

**Not tested by this tool:**

- a live Docker daemon, `up` against real containers, or real image builds through this tool (the Dockerfiles were built separately, see STATUS.md);
- a live download (the coordinator separately confirmed HTTP 200 and the exact hash for the pinned URL);
- TLS routing, real `root:42420` ownership (the permission test uses a group the non-root test user belongs to as a stand-in for 42420; the files are user-owned there, not root-owned), or a fresh end-to-end MARS install;
- non-Linux hosts, rootless Docker and Docker Desktop.

## Exact model egress hosts

Optional `MARS_LLM_EGRESS_HOSTS` in `site.env` is read by site validation and `render-env-spec` only. It accepts up to eight comma-separated exact lowercase public DNS hostnames, deduplicated in order against the application/runtime hosts. Empty is valid. URLs, ports, credentials, wildcards, IP literals, whitespace, empty entries and reserved placeholder/local suffixes are rejected without echoing the value. It is never forwarded into `private/compose.env` or Compose services. The operator obtains the hostname from the resolved model-provider connection; no endpoint discovery or resource creation occurs. Changing the list after rendering refuses to overwrite a differing private manifest. Register a new immutable MARS environment config and explicitly select it for subsequent VMs.
