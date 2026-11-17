# Sanitized verification summaries

Raw logs and infrastructure receipts remain private. See [status](../docs/STATUS.md) and [live validation](../docs/VALIDATION.md).

- Final live native 1/5/10: 16/16 completed, passing persisted outcomes, concurrent process/provider proof and automatic cleanup. A separate short diagnostic run also passed. Earlier failed attempts remain documented.
- Isolated evaluation-image regressions, real PostgreSQL 16: 94 passed; one Starlette dependency deprecation warning. Diagnostic capture explicitly attaches its handler because production disables normal log propagation.
- Portable stock Enterprise and Automation image builds: both passed (in-image installer and import checks).
- Source Enterprise regressions, real PostgreSQL 17: Opus reported 644 passed, mypy clean across 562 files, changed-file ruff clean. Includes lifecycle self-lock negative controls and diagnostics privacy.
- Automation source suite: 2691 passed, 60 skipped, 44 setup errors. Of these, 33 require Docker/MinIO and were not qualified; 11 startup-migration setup errors were covered by a separate 16/16 passing PostgreSQL test after the test shim was extended. Focused patch tests: 58 passed. Do not describe the full suite as clean green.
- Clean-release installer refusal/apply tests: 5 passed; migration up/down/stamp/re-upgrade and column-read-only gateway role checked on PostgreSQL 17.
- Operator driver boundary tests: 3 passed, covering wrong-org refusal before writes, ten independent dispatches with inactive retained history, and no replay after an ambiguous POST.
- Final stock Compose contract: 1 passed; public completion callback origin, private service API, public guest API, cap 10 and no app/worker Docker socket.
- Validation Python compile, ruff, formatting and preflight shell syntax: passed.

The operator driver was tested with mocks; the live cohorts used the Canvas native Test action on an inactive draft. This distinction matters when reproducing the API driver on a fresh customer installation.
