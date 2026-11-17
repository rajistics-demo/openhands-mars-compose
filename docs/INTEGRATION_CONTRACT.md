# Integration contract and DigitalOcean guidance

This document maps the DigitalOcean team's integration guidance to the implementation evaluated on 17 November 2026. **Implemented**, **observed** and **planned** are separate states. The integration is an evaluation adapter, pending the owning teams' review.

## Execution and ownership

Docker Compose runs OpenHands Enterprise, Automation, their databases and the MARS runtime gateway. DigitalOcean runs Agent Server and the job inside a hardware-isolated MARS microVM. A standard native Automation dispatch owns one sandbox and one MARS VM. Concurrent execution uses independent native runs; no parent/child or fan-out API is added.

The adapter implements the existing managed-sandbox and specification-injector interfaces. It preserves the Sandbox V1 API consumed by Automation and Canvas. Enterprise stores ownership, lifecycle intent and the provider mapping; Automation stores run status and completion metadata. The five provider integration points and their relationship to the earlier E2B implementation are described in [Integration seams](INTEGRATION_SEAMS.md).

## Guidance mapped to implementation

| DigitalOcean guidance | Implementation and evidence | Qualification |
|---|---|---|
| Pin an image digest and register a new immutable template for each release. | Enterprise/Automation **control-plane** base images are digest-pinned. MARS selects an operator-created environment config by ID. Operators should create a new config per release and avoid changing an existing config. The evaluated account ran the builtin `openhands` harness, reporting Agent Server 1.58.2. | **Partial.** A custom-image config was accepted but session creation rejected `AGENT_KIND_CUSTOM` on the evaluated Private Beta account. Config identity is not an image-digest guarantee. A supported pinned OpenHands runtime path requires DO follow-up. See [versions](VERSIONS.md). |
| Supply short-lived per-VM credentials and callback settings at startup or initialization. | A VM boots with deferred initialization. Enterprise initializes Agent Server through the private port-forward and supplies a signed JWT bound to the Enterprise sandbox ID, DO session ID, issuer, audience and expiry. Default session lifetime is eight hours, maximum 24 hours. The native run also receives an expiring owner API key, default one hour. The DO team token stays on the control plane. | **Partial.** The bootstrap init key is shared by the selected MARS config. The run key has owner API scope, rather than callback-only scope. Per-session JWT expiry is enforced, but seamless in-place rotation is unqualified. |
| Use callback-over-egress, exact hostnames and session-bound callback authentication. | Guest callbacks use public HTTPS. The operator config names exact allowed hosts; wildcards are not used. Agent Server's Enterprise callback/secret access uses the session-bound key. Native Automation completion uses its separate expiring run key. Server-to-server Automation calls retain the private Compose URL. | **Implemented with disclosed scope limits.** MARS adds platform/adapter hosts to the effective egress list. The portable two-origin topology also needs the runtime hostname when a job calls its own public runtime. Callback-only native credentials are not implemented. |
| Reach HTTP and WebSocket services using authenticated raw TCP over a private WebSocket tunnel, one WebSocket per TCP connection. | The runtime gateway authenticates the session before dialing DO, looks up the provider ID from Enterprise's stored mapping, and forwards to Agent Server port 8000 over MARS port-forward. HTTP streaming and inner WebSockets retain their protocols. Cookies, application Authorization and init credentials are stripped. Direct public VM URLs and preview ports are not exposed. | **Implemented and exercised.** HTTP, SDK WebSocket first-frame authentication and reconnect were observed. Ten native jobs passed. The gateway is MARS-specific; it is separate from an LLM gateway. Long-lived stream/load and failure recovery remain qualification work. |
| Preserve work through pause/resume; checkpoint before a planned suspend; reconnect TCP/WebSocket clients afterward. | Enterprise drives provider pause/resume and retains lifecycle/run records. Workspace and synthetic in-memory data survived a tested plain pause/resume. Reconnect requires a new transport connection and service health checks. | **Partial.** The adapter does not call the checkpoint API and makes no whole-machine durability or general process-restoration guarantee. DO persistent workspaces are not configured by this package. Key rotation and a dial concurrent with a pause remain open findings. |
| Keep task state and event history in Enterprise, with non-sensitive state in workspaces/checkpoints. | Provider ownership and lifecycle intent are durable Enterprise records. Native run status and `task_outcome` are durable Automation records. The completion patch merges metadata without replacing existing finish-tool metadata. | **Implemented for run records and outcomes.** Do not equate that with archiving every guest console/tool log or workspace file. Deleting a VM discards guest-only files and logs unless explicitly exported. Do not put long-lived credentials in workspace artifacts. Canvas 1.35.0 does not render the persisted custom-script `task_outcome`; the runs API exposes it. |

## Authentication boundaries

There are three distinct credential types:

1. **DO team token:** read from a file by app, worker and gateway; used only for provider control and port-forward. Never injected into the VM.
2. **Agent Server session JWT:** bound to one sandbox/provider session; used for authenticated runtime access and existing sandbox callback/secret endpoints. Stored as an encrypted value plus hash; the gateway reads the hash and mapping, not the encrypted plaintext key.
3. **Native Automation run key:** expires by default after one hour and is bound to the selected user/organization, but retains owner API scope. It authenticates the native completion callback and guest Enterprise API calls. It is not the DO token or the session JWT.

The bootstrap key is a fourth, config-level initialization secret. Replacing it with a per-VM bootstrap capability and replacing the owner-scoped run key with a callback-only capability are follow-up design items. This package does not claim those recommendations are fully satisfied.

Never enable query-string access logging on the runtime ingress: some supported WebSocket clients carry session authentication in the query. The gateway disables its own access log and emits only bounded method/status/error-class diagnostics. File-backed secrets and generated site configuration belong outside the integration checkout and release ZIP.

## Lifecycle and failure semantics

- Enterprise commits a durable creation intent before requesting a VM, then commits the provider ID before initialization. An ambiguous create is not blindly replayed. Adoption requires one exact name/config match; unresolved or duplicate matches require operator reconciliation.
- The gateway refuses a sandbox whose settled lifecycle state is paused. Its read does not lock the row, because the lifecycle worker probes idle status through this gateway while holding its own row lock. A dial already authorized when pause begins is not atomically fenced; open streams are not forcibly revoked. See [security review C4](SECURITY_REVIEW.md).
- A native callback persists `task_outcome` on successful and failed completions. A missing callback is handled by the native watchdog, rather than a synthetic success. Earlier transport failures are retained in the [validation record](VALIDATION.md).
- The default admission cap is ten sandboxes per identity. Exceeding it can pause the oldest RUNNING sandbox; it is not a queue or autoscaler. Confirm account quotas and isolate evaluation work from important conversations before increasing the cap.
- The tested runtime is Agent Server 1.58.2 with Enterprise's SDK 1.60.1. Deterministic execution passed; model inference and full SDK compatibility are not established by that result.

## Portability and rollout

The package applies narrow, hash-checked patches to released Enterprise 1.88.0 and Automation 1.30.0 images. It does not require the earlier AWS Lambda pool implementation or a copy of the evaluation machine. Its additive Enterprise migration is `213 → do_mars_001`; unsupported bases are refused rather than automatically rebased. Automation uses existing metadata and key-expiry columns.

Use the [installation guide](INSTALL.md) and [rollback procedure](ROLLBACK.md). A healthy stock baseline and backups precede switching the sandbox backend. Site DNS, TLS, DO account access and environment creation remain operator responsibilities. Kubernetes deployment and concurrency above ten are future qualification steps, not features certified by the Compose test.

## Code entry points for review

| Concern | Entry point |
|---|---|
| Config/template selection | [MARS spec injector](../enterprise/overlay/openhands/app_server/sandbox/do_mars_spec_service.py) and [config wiring patch](../enterprise/patches/openhands_app_server_config.patch) |
| Provider lifecycle, ownership and ambiguous creation | [Managed sandbox service](../enterprise/overlay/openhands/app_server/sandbox/do_mars_sandbox_service.py), [DO API client](../enterprise/overlay/openhands/app_server/sandbox/do_mars_client.py), [store patch](../enterprise/patches/openhands_app_server_sandbox_sandbox_store.patch) and [additive migration](../enterprise/overlay/migrations/versions/do_mars_001_add_provider_mapping.py) |
| Session credentials and deferred initialization | [JWT binding and expiry](../enterprise/overlay/openhands/app_server/sandbox/do_mars_credentials.py) and `_init_agent_server` in the managed service |
| Private HTTP/WebSocket transport | [Gateway](../enterprise/overlay/openhands/app_server/sandbox/do_mars_proxy.py), [TCP tunnel](../enterprise/overlay/openhands/app_server/sandbox/do_mars_tunnel.py) and [internal URL routing patch](../enterprise/patches/openhands_app_server_utils_docker_utils.patch) |
| Guest API URL and native run credentials | [Automation backend patch](../automation/patches/backends_cloud.patch), [run-key patch](../automation/patches/utils_api_key.patch) and [Enterprise expiry endpoint patch](../enterprise/patches/server_routes_service.patch) |
| Completion metadata persistence | [Automation completion-route patch](../automation/patches/router.patch) and [behavioral tests](../automation/tests/test_mars_package_patches.py) |

Installer/release tooling prepares the deployment; it does not alter these runtime contracts. Runtime source and prior native-run evidence remain unchanged by the packaging release.
