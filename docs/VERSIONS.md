# Versions, images and schema

| Component | Baseline | Source of truth | Verified |
|---|---|---|---|
| OpenHands Enterprise | 1.88.0, git `c6191be` | `ghcr.io/openhands/enterprise-server:1.88.0@sha256:cc67d10163e9e466b65d7a6e765a020fcc23ca036e78c6a6d846c458d1e2d9eb` | Index digest read anonymously from ghcr.io at 2026-11-17T05:45Z. Patched files are checked byte-for-byte by `enterprise/BASE_SHA256` |
| OpenHands Automation | 1.30.0, git `04d93d5` | `ghcr.io/openhands/automation:1.30.0@sha256:fb722b6fcde1a7fa67a15c40eb83af0ca23f1c111d0bc745f3b77a8b1bd33afa` | Same method. Patched files are checked by `automation/BASE_SHA256` |
| Enterprise migration head (base) | `213` (`213_add_started_by_to_v1_remote_sandbox.py`) | Stock 1.88.0 tree | `mars_apply.py` refuses any tree whose only head is not `213` |
| Package migration | `do_mars_001`, down `213` | `enterprise/overlay/migrations/versions/` | Upgrade, downgrade and re-upgrade were run on PostgreSQL 17. The migration graph test passes with a single head |
| Package migration (opt-in pool) | `do_mars_002`, down `do_mars_001` | `enterprise/overlay/migrations/versions/` | Adds `do_mars_pool_slot` only, independent of `v1_remote_sandbox`; read by no non-pool code path. `test_do_mars_pool_store.py`'s module fixture runs `downgrade()` then `upgrade()` against real PostgreSQL 17 before every test run, proving reversibility; migration head is a single chain (`213` → `do_mars_001` → `do_mars_002`) |
| Agent Server in the MARS VM | builtin `openhands` harness, reported as Agent Server **1.58.2** | DO MARS builtin template, chosen by the operator's `DO_MARS_CONFIG_ID` | This is the only tested path. Enterprise 1.88.0 itself is built against SDK **1.60.1**. The 1.58.2/1.60.1 skew is accepted for the deterministic smoke test only |
| Agent Server 1.60.1 image | `ghcr.io/openhands/agent-server:1.60.1-python@sha256:b43ee580c2177280c81cb807c9a559d7a14aa13b48dba5040132a6a637bb6d55` | ghcr.io | The registry digest is verified. **It is not usable as a MARS runtime pin on the evaluated account** (see below) |

## Why MARS image digests are not pinned

The DigitalOcean environment-spec reference documents `harness: custom` with an image digest and an argv entrypoint ([environment spec](https://docs.digitalocean.com/products/managed-agents/agent-harness-runtime/reference/environment-spec/)). On the evaluated Private Beta account the coordinator tested this live:

- Creating a config with a custom harness was accepted.
- Creating a session from it was rejected with HTTP 400 `agent_kind AGENT_KIND_CUSTOM not supported at Private Beta`.
- No VM was created, and the task-only config was deleted (verified with a 404).

DO's bring-your-own-template guide ([use a custom template](https://docs.digitalocean.com/products/managed-agents/agent-harness-runtime/how-to/use-custom-template/)) lists codex, opencode, claude-code, hermes and langgraph as supported bases. OpenHands is not among them, and a custom template does not replace the agent backend.

As a result, this package:

- **does not** ship or default to a custom harness;
- **does not** claim that pinning a MARS runtime digest works on this account;
- keeps the image choice in the operator's MARS config (`DO_MARS_CONFIG_ID`). The tested contract is the builtin `openhands` harness.

**Proposed DO follow-up for the evaluation teams (not sent by this package):** provide a supported path to run OpenHands Agent Server at a pinned immutable version in MARS. Acceptable forms are an immutable OpenHands builtin template per release, a custom-image (`AGENT_KIND_CUSTOM`) entitlement for this account, or a supported BYOT base for OpenHands. Until then, record the template version that each config reports, and register a **new** config for each release instead of editing an existing one.

## Images you build

`docker/enterprise.Dockerfile` and `docker/automation.Dockerfile` start from the digests above and apply this package. They never pull a personal or previously deployed image. After a build, record the resulting local image IDs or registry digests in your change log. This package cannot know those values in advance and does not invent them.

Both portable Dockerfiles successfully built on the AWS evaluation host from the pinned stock images, including in-image apply and import smoke checks. The live host used the same MARS modules on its existing evaluation image and migration chain. A complete fresh customer installation remains to be qualified.
