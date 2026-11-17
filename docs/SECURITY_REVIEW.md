# Security and correctness review

**Scope:** the final portable MARS modules and narrow patches against Enterprise 1.88.0 and Automation 1.30.0. This records agent code review and regression evidence, not a formal Enterprise team approval.

**Severity scale:** Critical means exploitable without credentials or a guaranteed data loss. High means a correctness or security blocker for the evaluation. Medium means a material limitation that must be disclosed. Low means hardening.

## A. Fixed in this package (blockers)

| # | Sev | Finding | Fix | Test |
|---|---|---|---|---|
| A1 | High | Batch sandbox lookup (`GET /api/v1/sandboxes?id=...`) ran `asyncio.gather` over **one** `AsyncSession`. On real PostgreSQL that is concurrent use of one session (`InvalidRequestError` → 500) whenever two or more ids are requested; reproduced on real PostgreSQL against the deployed body. | Serial row reads, then concurrent display mapping (`do_mars_sandbox_service.batch_get_sandboxes`). | `test_do_mars_package_fixes.py::test_batch_reads_ten_rows_serially_and_keeps_request_order` (real PostgreSQL). |
| A2 | High | The deployed secret-record path verified the JWT, but `get_sandbox_by_session_api_key` (authenticates guest callbacks and sandbox-scoped calls) matched only the SHA-256 key hash, so an **expired** per-session key still authenticated there. | Both paths use `_row_for_valid_session_key`: hash match **and** an unexpired JWT bound to the same sandbox id, DO session UUID, issuer and audience. | `test_do_mars_package_fixes.py::test_expired_session_key_cannot_authenticate_secret_reads`; binding tests in `test_do_mars.py`. |
| A3 | High | The gateway dialed **any** row that had a provider id, so a request to a PAUSED sandbox reached a MARS port-forward. A MARS port-forward can **auto-wake** a paused VM, which bypasses Enterprise lifecycle and billing intent. | The gateway reads `provider_resource_id`, `session_api_key_hash` and `lifecycle_state` with a plain read (no row lock; no plaintext key column). It authenticates the key first, then requires the settled state `running`, and only then dials. Explicit Enterprise resume is the only wake path. The check is **not atomic** with a concurrent pause (C4). | `test_do_mars_gateway_lifecycle.py`: settled paused/unknown rows on HTTP, header WS, query WS and SDK first-frame WS; every case asserts **zero** provider dials, and wrong-key callers learn nothing about the state. |
| A4 | Medium | The loopback tunnel accepted more than one local TCP client per authenticated provider WebSocket, and shutdown could hang because `wait_closed()` waits for active connections on Python 3.12+. | Exactly one TCP connection per forward (one WS per TCP); pumps cancelled before `wait_closed()`. | `test_do_mars_package_fixes.py::test_loopback_forward_accepts_exactly_one_local_connection`. |
| A5 | High | Migration `215` had `down_revision='214'`, the unpublished AWS pool migration, so it cannot install on stock 1.88.0. | Rebased as `do_mars_001` → `213` (1.88.0 head); non-numeric so it never shadows a future upstream `214`; idempotent `IF NOT EXISTS` DDL; installer refuses a tree whose head is not `213`. | `test_migration_graph.py`; `test_migration_is_rebased_on_release_head_and_survives_stamp_rollback`; PostgreSQL 17 up → down 213 → up → `stamp 213` → up (private regression receipt); `scripts/test_mars_apply.py::test_newer_enterprise_migration_head_is_refused`. |
| A6 | High | The deployed `config.py`, `docker_utils.py` and `sandbox_store.py` import or inject the unpublished AWS Lambda pool. | Only the MARS parts and the generic opt-in run-key expiry were extracted. `mars_apply.py` refuses any base file that is not byte-identical to 1.88.0. | `scripts/test_mars_apply.py` on a clean `git archive` of 1.88.0. |
| A7 | Medium | DO team token was a plain `str` injector field and could appear in config dumps/reprs. | `SecretStr`, `exclude=True`, read server-side from `*_FILE`. | `test_do_mars_package_fixes.py::test_injector_never_serializes_team_token`. |
| A8 | Medium | `max_num_sandboxes` defaulted to 2; admission pauses the owner's oldest RUNNING sandbox, so a third concurrent run by one automation identity would pause the first. | Default 10, documented; raise it for more than 10 concurrent runs per identity. | Default is visible in the injector; concurrency itself is coordinator-validated, not by this package. |
| A9 | Medium | Gateway path filter kept empty segments and compared case-sensitively, so `api//init` and `API/init` were not recognised as the init endpoint; `?`, `#` and control characters were not rejected. Init still required the stripped `X-Init-API-Key`, so this is defence in depth. | Empty segments dropped, case-insensitive `api/init`, reject `% ? #` and C0/DEL after iterative decoding. | `test_gateway_path_boundary_rejects_new_forms`, `test_gateway_path_boundary_allows_normal_paths`. |
| A10 | High | **Self-lock introduced by an earlier draft of A3 in this package** (found in coordinator review). That draft read the row `FOR SHARE NOWAIT` and returned 503 while any transaction held the row lock. The lifecycle worker's `check_sandbox` holds the row `FOR UPDATE` while `DoMarsSandboxService.get_idle_time` calls `/server_info` **through this gateway**, so every idle probe hit its own lock, and native GET/POSTs failed during every periodic check. | The row lock was removed: the gateway uses a plain MVCC read, which never waits on and never conflicts with the worker's lock. The settled-state check is kept. No shared lifecycle or Redis change was made. | `test_do_mars_lifecycle_idle_probe.py` (real PostgreSQL, real `check_sandbox`, real gateway app over ASGI; only the DO port-forward is replaced by a local guest stand-in). The worker's idle probe returns the guest value under its own lock, a native GET succeeds while the worker holds the lock, and a settled paused row is still refused with no dial. A negative control against the previous archive's gateway failed all three tests (`[None] == [42.0]`, `503 == 200`, `503 == 409`; private regression receipt). `test_row_lock_held_by_worker_does_not_block_or_refuse` replaces the old 503 test. |

**Rejected during review:** a time-based clearing of unresolved create intent. It treated "`list_sessions_by_name` is empty after startup+900s" as proof that no VM exists. That is **not** a provider guarantee, so it was reverted. An unresolved intent row is kept fail-closed: delete raises `SandboxError('MARS unresolved intent requires operator reconciliation')` until an operator reconciles it. The test `test_unresolved_create_intent_is_retained_for_reconciliation` covers both an empty listing and multiple listed sessions.

## B. Reviewed and acceptable for the evaluation

- **Gateway access:** the gateway reads only `provider_resource_id`, `session_api_key_hash` and `lifecycle_state`, with a plain read and no row lock, and compares hashes in constant time. It never writes a row. A least-privilege role needs column `SELECT` only (INSTALL.md step 4). On PostgreSQL 17 that role could not read `session_api_key`, `UPDATE` or lock rows.
- **Path, SSRF and header boundary:**
  - Only port 8000 is dialed, and previews are disabled.
  - The provider target is always `DoMarsClient.port_forward_url(row.provider_resource_id, 8000)`, built from the DB row. Caller input never chooses a host.
  - Paths are decoded up to 8 times. Any remaining `%`, `?`, `#`, control characters, `.`/`..` segments or `api/init` are rejected.
  - `Cookie`, `Authorization`, `X-Init-API-Key`, hop-by-hop, `Connection`-listed and `Sec-WebSocket-*` headers are stripped.
  - Redirects are not followed.
- **WebSocket:**
  - The `Origin` must equal the app origin when present.
  - When a header key and a query key are both given, they must match.
  - SDK first-frame auth is limited to 5 s and 8 KiB, and no provider dial happens before it is validated.
  - The query `session_api_key` is removed before forwarding.
- **Diagnostics privacy:** provider, tunnel and gateway failures log only validated fields: the method, a status or close code, the exception class and a `SESSION_STATUS_*` value. They never log exception text, URLs, headers, bodies, tokens, keys or identifiers, and never attach a traceback. A test enforces this with marker strings (DIAGNOSTICS.md). This also removed the old `sandbox_id` field from the cleanup error log. No diagnostic reads credentials from VM logs, and none is planned.
- **Lifecycle idempotency:**
  - The durable intent row and the provider UUID are committed before init.
  - There is a unique index on `(backend, provider_resource_id)`.
  - An ambiguous create is **never repeated**. It is adopted only when there is exactly one exact-name, exact-config match created after the intent and not owned by another row.
- **Bounded retries:**
  - Startup polling is bounded by `startup_timeout_seconds` (at most 900).
  - The gateway makes a single dial per request with a 20 s connect timeout.
  - There is no reconnection loop and no POST replay.
- **Secrets:**
  - The DO token, signing key and init key are read from `*_FILE`.
  - The session key is stored as hash plus encrypted value, following the stock pattern.
  - The validation job reports only the exception type, HTTP code and `URLError` reason class/errno, never URLs with keys.

## C. Open findings (not fixed; disclose to evaluators)

| # | Sev | Finding | Recommended follow-up |
|---|---|---|---|
| C1 | Medium | `DO_MARS_INIT_API_KEY` is **one shared bootstrap secret** in the MARS config, used by every VM's deferred `/api/init`. A guest that can read it could init another dormant guest it can reach. | Ask DO for a per-session init secret, or mint per-VM init keys at create time. |
| C2 | Medium | The native run key (`AUTOMATION_RUN_KEY_TTL_SECONDS=3600`) expires but has **owner-level API scope**. It is not limited to the callback. | Add a scoped, callback-only key type in Automation. |
| C3 | Medium | Egress is configured as the exact callback hostname, with no wildcard. The **platform default hosts are UNIONed** into the effective allowlist, so the guest can reach more than that one host. | Ask DO for an option to exclude platform defaults, or document the effective list per config. |
| C4 | Medium | **The in-flight pause race is unqualified.** The gateway's state check is a plain read, not atomic with Enterprise transitions. A dial authorized just before a pause commits can still reach the provider, and so could auto-wake the VM DO is pausing. A WS or stream that is **already open** when Enterprise pauses is not torn down by the gateway. Only *settled* paused rows are guaranteed to be refused. A lock-based fix conflicts with the lifecycle worker's own idle probe (A10). | Bounded handoff: have the idle probe call the provider directly instead of the public gateway, then gate dials on a short-lived lease, and have the gateway close connections on a pause notification. Each needs its own PostgreSQL regression. |
| C5 | Medium | Pause and resume use the DO pause API. Workspace survival and synthetic in-RAM survival were **observed**. This is **not** a durable checkpoint guarantee, and the checkpoint API is not used. | Integrate the DO checkpoint (whole machine, files, live memory) once it is available. After a resume, reconnect all TCP/WS connections. |
| C6 | Medium | When the session JWT expires (default 8 h, max 24 h), a **fresh VM** is required. Seamless key rotation is unqualified. | Add an Agent Server key-rotation call through deferred init. |
| C7 | Medium | The builtin `openhands` harness runs Agent Server 1.58.2, while Enterprise 1.88.0 targets SDK 1.60.1. Custom harness or digest pinning is rejected on Private Beta (`AGENT_KIND_CUSTOM not supported`). | This is a DO follow-up, see VERSIONS.md. Re-qualify the setup when DO provides a pinned OpenHands template. |
| C8 | Low | The legacy WS query key could appear in front-proxy access logs. The gateway disables its own access log. | Disable query logging on the TLS terminator for the runtime hostname. |
| C9 | Low | A delete with an unresolved intent raises and is retried every lifecycle sweep. It is periodic, not a tight loop, but it creates noise until an operator reconciles it. | Add an operator reconcile command or runbook (ROLLBACK.md, "Unresolved intents"). |
| C10 | Low | `mars-gateway` uses the same image as the app and therefore carries the full app code. | Build a slim gateway image later. |
