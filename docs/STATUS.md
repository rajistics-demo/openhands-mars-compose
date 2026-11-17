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

HTTP, SDK WebSocket first-frame authentication/reconnect, callbacks, and plain pause/resume with workspace and synthetic memory survival were also observed earlier. No checkpoint durability or model inference is claimed.

## Not validated / remaining integration blockers

0. **Earlier failures remain unexplained:** the withdrawn nested-polling ten-run attempt failed, and one earlier callback hit a guest proxy CONNECT 503. Final ordinary-job cohorts passed; longer soak and failure/recovery qualification remain. See VALIDATION.md.
1. **Fresh customer deployment not yet qualified.** Both portable stock-image builds passed on AWS. Customer onboarding, TLS routing, account-specific template access and deployment against a fresh database still require the INSTALL.md checks.
2. **No MARS runtime image pin.** `AGENT_KIND_CUSTOM` is rejected on Private Beta. The OpenHands Agent Server 1.60.1 digest is verified in the registry only. The DO follow-up is in VERSIONS.md.
3. **Agent Server version skew:** builtin 1.58.2 against Enterprise SDK 1.60.1. Only the deterministic smoke test exercises it.
4. **No LLM inference validated.** There is no model or capacity qualification, and no production qualification.
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

The setup/release suite passed 46 tests. Independent clean-directory preparation passed against the real Docker/Compose CLI without starting containers. Full fresh-install onboarding and end-to-end MARS validation remain customer qualification steps; prior live 1/5/10 evidence describes the existing evaluation deployment.
