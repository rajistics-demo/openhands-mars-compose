# OpenHands Enterprise × DigitalOcean MARS: evaluation package

This package adds a DigitalOcean MARS (Harness Runtime) sandbox provider and a runtime gateway to **OpenHands Enterprise 1.88.0**, and adds the matching **Automation 1.30.0** patches. Docker Compose runs the control plane. MARS runs the jobs, one VM per sandbox, and a standard native Automation run gets one MARS VM.

**Live result:** 1, 5 and 10 independent native jobs passed with persisted outcomes and automatic VM cleanup. At ten jobs the four-vCPU Compose host used 9.6% p95 CPU and at most 3.90 GiB RAM. See [validation and screenshots](docs/VALIDATION.md), then [setup](docs/INSTALL.md), [five integration seams](docs/INTEGRATION_SEAMS.md) and [remaining limits](docs/STATUS.md). This is a version-pinned evaluation package.

```
enterprise/            new MARS modules (overlay/), narrow patches/ for 5 stock files, tests/,
                       BASE_SHA256 (1.88.0 originals), PATCHED_SHA256 (expected result), MIGRATION_HEAD
automation/            patches/ for 4 stock files, tests/, BASE_SHA256, PATCHED_SHA256
scripts/mars_apply.py  stdlib-only, fail-closed, version-checked installer (used by the Dockerfiles)
scripts/preflight.sh   read-only preflight
scripts/test_mars_apply.py  clean-base portability tests
docker/                Dockerfiles FROM the released images, pinned by verified digest
compose/               docker-compose.mars.yml overlay + mars.env.example (placeholders only)
validation/            deterministic one-run / one-VM native check (no LLM)
docs/                  INSTALL, ROLLBACK, VERSIONS, INTEGRATION_SEAMS, SECURITY_REVIEW, DIAGNOSTICS, STATUS
test-results/          sanitized verification summary; raw logs stay private
SHA256SUMS             per-file hashes
```

**Licensing.** The patches modify licensed OpenHands Enterprise files. You may apply them only to an Enterprise image you are licensed to use, and nothing here grants a licence. The files under `enterprise/overlay` are new modules for that product and fall under the same terms. Automation is MIT.

The package contains no credentials, account ids or real hostnames. Every example uses `example.invalid`.

## Reproducing the offline tests

These steps need a licensed 1.88.0 source tree, the Enterprise dev dependencies (`uv sync --all-groups`) and PostgreSQL. The Enterprise test fixtures normally start PostgreSQL through Docker.

```bash
mkdir -p /tmp/e188
git -C <enterprise-1.88.0 checkout> archive 1.88.0 | tar -x -C /tmp/e188
python3 scripts/mars_apply.py enterprise /tmp/e188
cp -r enterprise/tests/unit/. /tmp/e188/tests/unit/
(cd /tmp/e188 && patch -p1 < "$OLDPWD/enterprise/tests/test_api_key_store_expiry.patch")
(cd /tmp/e188 && uv run pytest tests/unit/app_server/test_do_mars*.py tests/unit/server/routes/test_service_run_key_expiry.py \
   tests/unit/storage/test_api_key_store.py tests/unit/test_migration_graph.py tests/unit/app_server/test_sandbox_store.py)
```

For Automation, run `mars_apply.py automation <1.30.0 checkout>`, copy `automation/tests/*` into its `tests/`, then run `uv run pytest tests`. To run the installer portability tests, set `ENTERPRISE_188_TREE` and `AUTOMATION_130_TREE` and run `python -m pytest scripts/test_mars_apply.py`.

## Distribution

Use this repository as an overlay beside the [official Enterprise Compose installation](https://docs.openhands.dev/enterprise/docker-compose). Obtain the **1.88.0** release tree for this package; do not apply these patches to current main. Keep the stock Compose file and your site settings, build the two images here, then add `compose/docker-compose.mars.yml` to your Compose file list. A fork of the whole Enterprise repository or a copy of the evaluation machine is unnecessary. No credentials or customer-specific infrastructure are distributed.

Check the merged callback and control paths without starting containers:

```bash
ENTERPRISE_COMPOSE=/path/to/enterprise-1.88.0/docker-compose.yml python3 scripts/test_compose_contract.py
python3 validation/test_driver.py
```
