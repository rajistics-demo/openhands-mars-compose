# Status: what is validated and what remains

This package is a **customer evaluation** package. It is not a production release.

## Validated by this package (offline, reproducible)

Raw logs remain private; this document and `test-results/README.md` contain sanitized summaries.

- **Enterprise regression set** on 1.88.0 plus the overlay, run on native PostgreSQL 17: **644 passed**. It covers the MARS provider, gateway, lifecycle gate, diagnostics privacy (12 tests), the lifecycle-worker idle probe through the gateway under the worker's own row lock (real `check_sandbox`, real PostgreSQL), package fixes, sandbox store, run-key expiry route, API key store, migration graph, the remote, Docker and E2B sandbox services, all lifecycle contract/rules/sweep/tasks/config tests, quota ownership and SaaS lifespan. mypy 1.15.0 reported "no issues found in 562 source files", and ruff is clean.
- **Automation full suite** on 1.30.0 plus the patches: **2691 passed, 0 failed, 60 skipped**. There were 44 setup errors:
  - 33 in S3/MinIO storage and upload tests. These require Docker/MinIO and were not qualified in the worker environment.
  - 11 in startup-migration tests. These passed (16/16) after the PostgreSQL shim was extended.

  The focused MARS patch tests passed 58/58. ruff and pyright are clean on the changed files.
- **Clean-base installer:**
  - It applies to a clean `git archive` of 1.88.0 and to a clean 1.30.0 tree.
  - It refuses a second apply, a modified base file and a newer migration head, and writes nothing in each of those cases.
  - 5 of 5 tests passed.
- **Migration on PostgreSQL 17:** up, down to 213, up, `stamp 213`, up. Stock 1.88.0 rejects a `do_mars_001` database, as documented.
- **Compose overlay** merged with the stock 1.88.0 Compose file (`docker compose config` rc=0).
- **Gateway role privileges** were checked on PostgreSQL 17.

## Observed live

See [VALIDATION.md](VALIDATION.md) and the sanitized `evidence/` receipts. Final measured **1/5/10 independent native runs passed**, with concurrent provider/process proof, persisted passing outcomes and automatic provider cleanup for every run. Two portable stock-image builds passed. The live evaluation's additive MARS schema used its existing migration chain; a complete fresh customer deployment is still a qualification step.

HTTP, SDK WebSocket first-frame authentication/reconnect, callbacks, and plain pause/resume with workspace and synthetic memory survival were also observed earlier. A real Gemini conversation and a post-reload follow-up subsequently passed on one MARS VM; see [interactive LLM validation](LLM_VALIDATION.md). No checkpoint durability is claimed.

## Not validated / remaining integration blockers

0. **Earlier failures remain unexplained:** the withdrawn nested-polling ten-run attempt failed, and one earlier callback hit a guest proxy CONNECT 503. Final ordinary-job cohorts passed; longer soak and failure/recovery qualification remain. See VALIDATION.md.
1. **Fresh customer deployment not yet qualified.** Both portable stock-image builds passed on AWS. Customer onboarding, TLS routing, account-specific template access and deployment against a fresh database still require the INSTALL.md checks.
2. **No MARS runtime image pin.** `AGENT_KIND_CUSTOM` is rejected on Private Beta. The OpenHands Agent Server 1.60.1 digest is verified in the registry only. The DO follow-up is in VERSIONS.md.
3. **Agent Server version skew:** builtin 1.58.2 against Enterprise SDK 1.60.1. The deterministic smoke test and one real Gemini conversation with a follow-up exercise it; broader SDK compatibility is unqualified.
4. **Limited LLM coverage.** One Gemini conversation and a post-reload follow-up passed on one VM. Multi-model, concurrent inference, long-running workload and production qualification remain.
5. **No checkpoint API integration.** Expired session keys require a fresh VM (seamless rotation is unqualified).
6. **Shared bootstrap init secret.** The run key is owner-scoped rather than callback-only. Egress includes the platform default hosts. See SECURITY_REVIEW.md, part C.
7. **Unresolved create intents** need manual reconciliation by an operator (ROLLBACK.md).
8. **Canvas does not show `task_outcome`.** the released Canvas 1.35.0 run display reads `run_metadata.finish_tool_response`, not `run_metadata.task_outcome`. A custom-script native run therefore shows "Successful" with "No task outcome reported" even though `task_outcome` is persisted. Inspect it with `GET /api/automation/v1/{automation_id}/runs` (field `run_metadata.task_outcome`) or a read-only query on the Automation database. The run history is retained; guest VM logs are gone once the VM is cleaned up. This package does not change Canvas.
9. **In-flight pause race** (SECURITY_REVIEW C4): only settled paused rows are refused; a dial authorized just before a pause commits is unqualified.
10. **Customer overlays on the same files.** If the base already carries other overlays on the patched files, the installer refuses to run instead of merging. Rebase those overlays by hand onto `patches/`.

## Explicitly not in this package

- Fan-out, coordinator or child-run creation. Concurrency means N independent native runs, not a ten-child feature.
- The unpublished AWS Lambda pool and its migration 214.
- The previous personal AWS image.
- Any Enterprise source beyond the new MARS modules and the narrow patches.

## Customer package — 2026-11-17

The versioned evaluation bundle includes a staged Python installer, a checksum-verified fetch of the official Enterprise Compose file, local image builds and guarded baseline/MARS activation. The customer README and INSTALL guide cover prerequisites and private configuration. INTEGRATION_CONTRACT maps DigitalOcean's recommendations to implemented behavior and outstanding qualifications. INSTALL_MANUAL preserves the existing-stack overlay path.

The current setup/release suite passed 57 tests. Independent clean-directory preparation passed against the real Docker/Compose CLI without starting containers. Full fresh-install onboarding and end-to-end MARS validation remain customer qualification steps; prior live 1/5/10 evidence describes the existing evaluation deployment.

## Interactive conversation follow-up — 2026-11-17

The MARS readiness probe now sends the existing session key to the authenticated gateway's `/alive` route. Other providers retain the stock shared probe. Nine new regression tests cover real shared startup polling, authenticated gateway routing, refused/missing keys, boot retries and diagnostic privacy. The final focused MARS set passed 63 tests on PostgreSQL 16; a broader relevant regression run passed 390 tests before the final diagnostics capture-fixture adjustment. The final 63-test run includes that adjustment. These counts are separate from the earlier baseline suites above.

The installer adds optional exact `MARS_LLM_EGRESS_HOSTS` entries to the rendered MARS manifest only. The approved new immutable environment config and one VM were used for native conversation startup, actual model inference, terminal/file tools, independent provider-side file verification, and a successful UI follow-up after reload. Initial network-policy and hosted-budget failures were retained; they are not counted as passes. Details and limitations are in [LLM_VALIDATION.md](LLM_VALIDATION.md).

## Opt-in warm pool and review fixes — 2026-11-24

Added the opt-in, disabled-by-default warm pool (`do_mars_pool_config.py`/`do_mars_pool_store.py`/`do_mars_pool_reconciler.py`/`do_mars_pool_worker.py`, migration `do_mars_002`) and wired `DoMarsSandboxService.start_sandbox` to claim from it ahead of creating fresh VMs. See [INTEGRATION_SEAMS.md](INTEGRATION_SEAMS.md) row 6 for the full design, what was executed against real PostgreSQL 17 (`do_mars_pool_store.py`, `do_mars_pool_reconciler.py`: concurrency, atomicity, the reconciler's crash-recovery state machine) versus what is static/contract-level only (the `start_sandbox` claim call site itself, which needs the real `openhands.agent_server`/`ManagedSandboxService` base classes this standalone package does not have).

This pass also fixed a binding defect found in review: the claim query and the pool worker's own slot-creation call were keyed on a separately-settable `DO_MARS_POOL_CONFIG_ID` (defaulting to the literal string `"default"`) instead of the provider's real config id for the spec, so a config rotation could leave stale-config pool VMs claimable for requests that had already moved to a new config. Fixed by removing that separate variable entirely - both call sites now use `DO_MARS_CONFIG_ID`/`spec.config_id`, the same value the non-pool create path already passes to the provider - with a worker-level regression test executed here and a service-level one added to the unexecuted-here `test_do_mars_package_fixes.py`. The reconciler's `DB_HOST`/`DB_PORT` were also made overridable (previously hardcoded to the bundled `db` Compose service) for bring-your-own managed PostgreSQL, per the existing Enterprise DB contract; see the new "Warm pool" section of INSTALL.md for the session-pinning requirement this carries.

**Run in this environment** (a lightweight `.venv-mars`, not the full licensed Enterprise 1.88.0 tree): `ruff check` (no new findings beyond this package's 74 pre-existing ones, confirmed by diffing against the prior commit), `python -m py_compile` on every changed file, and the full collectable pytest suite - 67 passed, 34 skipped (skips are the documented `MARS_STOCK_COMPOSE`/`ENTERPRISE_188_TREE`/`AUTOMATION_130_TREE` licensed-input gaps, same as prior entries), 0 failed, including all `test_do_mars_pool_store.py`/`test_do_mars_pool_reconciler.py` tests against the real PostgreSQL 17 container already running in this worker. `installer/mars_release.py sums --check` passes against the regenerated `SHA256SUMS`, and `enterprise/PATCHED_SHA256` was regenerated for the two changed overlay files and independently re-verified by running `mars_apply.py --check` against a synthetic component/root pair (not the real Enterprise tree, which is unavailable here).

**Remains unverified**, same gap as the rest of this package's pool work: `_start_sandbox_from_pool` and the pool-related sections of `test_do_mars.py`/`test_do_mars_package_fixes.py`/`test_do_mars_gateway_lifecycle.py`/`test_do_mars_lifecycle_idle_probe.py`/`test_do_mars_startup_health_auth.py` (uncollectable here, `ModuleNotFoundError: No module named 'openhands.agent_server'`), the full `test_do_mars_package_fixes.py` migration-head regression test now asserting `do_mars_002` as head (updated by static inspection of the two migrations' `revision`/`down_revision` fields, not executed), `scripts/test_mars_apply.py` against a real `ENTERPRISE_188_TREE`/`AUTOMATION_130_TREE`, and 50-sandbox-concurrency pool benchmarking (explicitly deprioritized by the requester for this round). No pilot Canvas patch was available during this pass; that remains a separate, later step once provided.

## Canvas pilot patch integration and build/install packaging — 2026-11-24

The delivered `mars-canvas-pilot.patch` (tested source patch) was inspected before integration. Every hunk in it was confirmed, file by file, to already be present and byte-identical in this branch: reverse-apply dry-runs (`patch -R --dry-run`) came back clean for every modified file it touches, and a direct byte diff of its added-lines content against `canvas/authenticated-preview.patch`, `enterprise/overlay/openhands/app_server/sandbox/do_mars_preview.py`, the `preview_domain`/`exposed_urls`/`supports_runtime_pause` wiring in `do_mars_sandbox_service.py`, and the three `enterprise/patches/*.patch` files confirmed exact matches (the one nominal `patch` failure, in `do_mars_sandbox_service.py`, was pure context drift from this branch's own later `pool_config` field sitting between the patch's two hunks - the patched content itself, `preview_domain: str | None = None` through the injector's `preview_domain=preview_domain(...)` call, is present and correctly layered under the pool field). No backend or frontend-patch-file changes were needed this pass.

What remained, and was built this pass, is the install/packaging route for it, following the analogous static-server pattern in the `openhands-daytona-compose` reference repository (cloned read-only for comparison, never pushed to, and not otherwise touched):

- [`compose/docker-compose.canvas-pilot.yml`](../compose/docker-compose.canvas-pilot.yml): a new opt-in overlay that swaps the stock `canvas-server` container's served bundle for a bind-mounted `CANVAS_PILOT_BUILD` directory, with its command explicitly set to `--lock-to-cloud ${WEB_URL:?...}` - the same required-var-or-fail pattern as the rest of this package's compose files, not a default that could silently resolve to the wrong origin. The image itself is untouched.
- [`docs/CANVAS.md`](CANVAS.md): the reproducible build recipe (pinned Canvas commit `934612b3b6cee05d3a4400b98b48a9c69215401d`, `git apply --check` before `git apply`, `npm ci`, the two patch-targeted vitest files, then `VITE_BASE_PATH=/canvas VITE_CANVAS_RECOVERY_PILOT_ORIGIN=<real APP_ORIGIN> npm run build`), both installer-managed and manual Compose setup for the prebuilt bundle, and the per-conversation opt-in tag.
- [`compose/Caddyfile.example`](../compose/Caddyfile.example): added the previously-missing concrete preview-routing blocks for both documented choices - a wildcard `*.preview.app.example.invalid` vhost for a shared install, and a commented single named-sandbox-host alternative for a disposable test - both forwarding to the same gateway upstream as the runtime vhost. Validated for real with a pinned `caddy validate` (v2.9.1, downloaded from the official GitHub release for this check only, not installed or run as a service): `Valid configuration`, with only the same pre-existing cosmetic indentation-style warning the original file already had.
- `docs/INSTALL.md`'s "Enable Canvas previews" section now points at these concrete artifacts instead of prose only, and its four remaining stale `do_mars_001`-as-final-head mentions (missed in the prior pass) were corrected to `do_mars_002`/both revisions. `docs/INSTALL_MANUAL.md` and `docs/INSTALLER_CONTRACT.md` have the same category of stale mentions (found during this pass) and were deliberately left untouched - out of scope for this Canvas-focused round per the requester's explicit "postpone unrelated polish."

**Run in this environment**: `docker compose ... config --format json` (real Docker Compose v5.8.0, no daemon/containers started) against the new overlay layered onto a synthetic stub `canvas-server` service standing in for the licensed stock file - new `installer/tests/test_canvas_pilot_compose.py`, 3 passed, asserting the merged command's `--lock-to-cloud` value, that it appears exactly once (no leftover stock occurrence), the bind-mount source/target/read_only, and that both `WEB_URL` and `CANVAS_PILOT_BUILD` are hard-required (missing either fails the merge with a message naming the variable). `enterprise/tests/unit/app_server/test_do_mars_browser_preview.py` was also re-run directly against this branch's real `do_mars_proxy.py`/`do_mars_preview.py` (not mocked business logic) - 3 passed, covering unauthenticated denial, cross-sandbox cookie rejection, and that a paused or rekeyed sandbox is never dialed (`forward.assert_not_called()`). Full `installer/tests/` suite: 32 passed, 28 skipped (same licensed-input gaps as before), 0 failed. `ruff check .`: 74 errors, unchanged from the pre-existing baseline (confirmed by first finding and fixing one new finding in the test file itself, a missing `check=` kwarg). `installer/mars_release.py sums --write`/`--check`: 93 files, matches.

**Remains unverified**: the two vitest files named in `docs/CANVAS.md` and the pinned `npm run build` itself (no Canvas source tree or Node 24 toolchain in this environment - the recipe was transcribed and cross-checked against the daytona reference's equivalent doc, not executed here); the stock `canvas-server` image's actual CLI accepting these exact flags end-to-end (the compose test proves the overlay's own YAML and substitutions, using a synthetic stand-in service, not the real image); any live browser verification of the Pause control or embedded preview (barred in this pass - no live DigitalOcean infrastructure, no production access); and everything already listed as unverified in the prior pool entry above, which this pass did not touch.

The final installer accepts the prebuilt Canvas directory in `site.env` and activates the pool service when enabled. The AWS installer suite passed 50 tests with 28 skipped. The skipped cases require the pinned licensed Compose inputs.


## Live MARS warm pool and Canvas replay

The combined installer was exercised on an isolated AWS Compose host with
live two-vCPU, four-GiB MARS VMs and the pinned Canvas bundle. The installed
stack reached migration `do_mars_002`. The warm case assigned the exact
provider VM observed in the hot pool before the conversation was requested.

| Browser milestone | Fresh VM | Prestarted VM |
| --- | ---: | ---: |
| Native conversation request to Ready | 36.58 s | 22.78 s |
| Reopen after Pause to visible history | 0.16 s | 0.15 s |
| Reopen after Pause to a successful embedded preview request | 11.52 s | 10.65 s |

Each column is one run. The browser ran on the AWS test host and the
conversation used a synthetic history marker without model inference. In
both cases reopening kept the conversation, provider VM and preview process
and preserved its in-memory counter. An authenticated preview request
while paused left the provider VM paused.

A spare was moved into the paused reserve, then back to hot on the same
provider VM. Setting the targets and cap to zero drained the spare.
Provider-side confirmation and an empty inventory were both observed.

The test used bundled PostgreSQL. Managed PostgreSQL setup and draining
before a provider configuration rotation are described in [INSTALL.md](INSTALL.md#warm-pool).
Workspace recovery after provider expiry and concurrent capacity are
separate qualification work.
