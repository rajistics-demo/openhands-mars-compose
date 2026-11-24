# Versions, images and digests (DOKS package)

Same method as `../../docs/VERSIONS.md` (the 1.88.0 Compose baseline):
read directly from the registry's own manifest API, not derived or
guessed, and recorded here as a citable fact rather than embedded only
in a comment.

| Component | Baseline | Source of truth | Verified |
|---|---|---|---|
| OpenHands Enterprise | 1.97.1 | `ghcr.io/openhands/enterprise-server:1.97.1@sha256:fdf5df25a89e0fa7187829b41b8b3cd86f5eb99d2420c71dae4dd16e6ae37d07` | OCI index digest read from `GET /v2/openhands/enterprise-server/manifests/1.97.1` (`Accept: application/vnd.oci.image.index.v1+json`) against `ghcr.io` directly in this evaluation worker at 2026-11-24T16:59:10Z; the response's `Docker-Content-Digest` header matches this digest exactly, and the index lists both `linux/amd64` and `linux/arm64` platform manifests (plus their attestation manifests) under it |
| Official `openhands` Helm chart | `0.119.0` (`appVersion` `1.97.1`) | `oci://ghcr.io/openhands/helm-charts/openhands` | Published chart tarball sha256 recorded in `doks/docs/HELM.md`'s render-proof section |
| Canvas (`agent-canvas` subchart default) | `1.45.0` | `ghcr.io/openhands/agent-canvas:1.45.0` (this chart's own stock default - not overridden by `openhands-mars.values.yaml`) | Confirmed by reading the published `agent-canvas` subchart's own `values.yaml` `image.tag` directly |
| Canvas pilot patch (Pause + authenticated preview), original | Evidenced against Canvas **1.35.0** commit `934612b3b6cee05d3a4400b98b48a9c69215401d` | `../../canvas/authenticated-preview.patch`, `../../docs/CANVAS.md` | Source-level only; see `../../docs/CANVAS.md` |
| Canvas pilot patch, rebased for this chart's **1.45.0** default | Evidenced against Canvas 1.45.0 commit `47eed012171230b059c3f5cbf3798bded74ea248` | `../../canvas/authenticated-preview-1.45.0.patch`, `../../docs/CANVAS.md` "1.45.0 pin for the DOKS Helm chart" | 44 focused Vitest tests passed on a fresh checkout. Typecheck, lint and frontend build passed. The patched OCI image built and served the updated bundle over HTTP. |
| `doks/docker/enterprise.Dockerfile` | Pins `ENTERPRISE_BASE` to the digest above by default | `doks/docker/enterprise.Dockerfile` | Digest match confirmed as above. **Built successfully** against the real pulled image in this worker: `mars_apply_doks.py` applied 25 files under `/app` (its own byte-for-byte base-hash check against the real 1.97.1 tree passed - it fails closed otherwise), and the import smoke check (`do_mars_proxy`, `do_mars_sandbox_service`, `do_mars_pool_store`) succeeded against the real licensed tree, reporting `OpenHands SDK v1.63.1` |

`ENTERPRISE_BASE` is a default-valued build arg, not a hardcoded `FROM`
line, so a future release (or a site with its own mirrored registry) can
override it with `--build-arg ENTERPRISE_BASE=...` without editing the
Dockerfile - the same override mechanism `docker/enterprise.Dockerfile`
documents for 1.88.0.

**Full pull and build completed:** `docker pull` of this exact digest
succeeded anonymously (no registry login) in this evaluation worker -
`docker images` shows the resulting local image at digest
`fdf5df25a89e` - and `docker build -f doks/docker/enterprise.Dockerfile .`
against it completed successfully (tagged locally as
`mars-doks-enterprise:smoke`), including the Dockerfile's own
`mars_apply_doks.py` base-hash-checked overlay apply and the three-module
import smoke check, both passing against the real licensed tree. Raw
build log kept local to this worker only (not committed - see the task's
own evidence-handling constraints); this file is the durable record of
what ran and what it reported.

**Retrying the overlay unit tests against this real image - attempted,
blocked, not pursued further this session:** the built
`mars-doks-enterprise:smoke` image is a production runtime image with no
`tests/` tree and no dev dependency group installed (`pytest` absent;
`pyproject.toml`'s `[dependency-groups].dev` pins `pytest==9.2.0` but that
group is not installed in this image, by design - production images
strip it). Reconstructing the full enterprise test harness (conftest
fixtures, `postgres_testdb`, the dev dependency group at its exact
`uv.lock`-pinned versions) inside this stripped runtime image was not
attempted further in this session in favor of the Canvas work requested
next; `doks/enterprise/tests-overlay/unit/app_server/test_do_mars_*.py`
still need a source checkout with the dev group installed (as the
existing `.venv-mars`-based attempts already found) to run for real,
not this built image.

## Canvas pilot port status

The Pause/authenticated-preview source patch
(`canvas/authenticated-preview.patch`) is tested and evidenced only
against Canvas 1.35.0 commit `934612b3b6cee05d3a4400b98b48a9c69215401d` -
see `docs/CANVAS.md` for the full build/test recipe. `openhands-mars.values.yaml`
deliberately does not set `agent-canvas.image.repository`/`tag`, so a DOKS
install using this package's values overlay keeps the chart's stock,
un-patched Canvas image and simply does not get the Pause/preview pilot
features - it does not regress stock Canvas.

**Real source investigation performed this session (not a guess):**
first, which commit actually builds `agent-canvas:1.45.0` was resolved
precisely rather than assumed - the image config blob's own OCI labels
(`org.opencontainers.image.source`/`.revision`, read from the real
`ghcr.io` manifest, not inferred) point at
`https://github.com/OpenHands/OpenHands` commit
`47eed012171230b059c3f5cbf3798bded74ea248` ("chore(main): release
1.45.0"), **not** the separately-existing `OpenHands/agent-canvas` repo
(which only goes up to `v1.6.1` and is unrelated to this image's
versioning - a real dead end this session ruled out by checking its tags
directly, worth recording so it is not re-investigated from scratch).

A shallow, blob-filtered clone was checked out at that exact commit and
`git apply --check` of the unmodified patch was run against it for real
(not simulated): 6 of 9 files apply cleanly (2 new files, 4 existing
files with only line-number offsets), and 3 fail outright -
`__tests__/.../conversation-name-with-status.test.tsx`,
`src/hooks/mutation/use-unified-stop-conversation.ts`, and
`src/i18n/translation.json`.

**The reason the hook file fails is the important finding, not the
conflict itself:** reading the real current
`use-unified-stop-conversation.ts` and its `pauseConversation` helper
(`conversation-mutation-utils.ts`) shows upstream has *independently*
grown its own cloud/local-aware pause path since 934612b3 - an
`isCloud` check, cloud-specific toast copy
(`TOAST$STOPPING_SANDBOX`/`TOAST$SANDBOX_STOPPED`), and, in
`pauseConversation` itself, exactly the "resolve the sandbox id and call
`pauseCloudSandbox`" behavior our patch added - but by freshly calling
`fetchConversationData(conversationId)` at pause time rather than
trusting a client-cached `sandboxId`/`backendId`/`orgId` the way our
patch's `isSelectedRuntimeScope` staleness guard did. That is a
different, arguably simpler fix for the same race our patch was
guarding against, landed by someone else in the meantime. Confirmed
`sandbox_supports_runtime_pause` and any dedicated, separately-gated
"Pause" button do not exist anywhere in this real tree (`grep` across
`src`/`__tests__` found nothing) - so upstream's unified Stop button
already behaves like a cloud-aware pause, but there is still no distinct
capability-gated Pause control, and the embedded authenticated live
preview (`PilotRecoveredPreview`) is confirmed entirely absent upstream.

**Conclusion: re-applying the old patch's hook/test hunks as-is would be
wrong, not just mechanically blocked.** It would reintroduce a
client-side staleness guard upstream has already replaced with a
server-derived one, likely duplicating or conflicting with the real
current pause path rather than filling a gap. Finishing this port
correctly needs a deliberate redesign of the hook/test hunks against
upstream's current `pauseConversation`/`isCloud` design (keep upstream's
server-derived sandbox resolution, add only the still-missing
capability-gated Pause button and i18n keys on top of it) - not a
line-by-line carry-forward of the 1.35.0-era hunks. The two cleanly
applying new files (`pilot-recovered-preview.tsx` and its test) remain
the most directly portable part of the patch, since that feature has no
upstream equivalent at all.

**Done, this session:
[`canvas/authenticated-preview-1.45.0.patch`](../../canvas/authenticated-preview-1.45.0.patch).**
Exactly the redesign above, not a carry-forward: `use-unified-stop-conversation.ts`
is untouched (zero lines changed) - the new Pause button in
`conversation-name-with-status.tsx` calls the real, already-existing
`useUnifiedPauseConversation` export from that file directly, adding only
a `canPauseRuntime` gate (`backend.kind === "cloud"`,
`sandbox_supports_runtime_pause === true`, the active conversation ID
matches this route's, `sandbox_status === "RUNNING"`) and the button's own
markup/i18n key (`COMMON$PAUSE_RUNTIME`) on top of it. The two previously-blocked
files (`use-unified-stop-conversation.test.tsx`, `translation.json`'s
other now-superseded keys) were likewise not carried forward - the test
file is exercised only as an unmodified regression check (still 5/5
passing, confirming this change didn't touch its behavior), and only the
one still-needed i18n key was added. `pilot-recovered-preview.tsx` and
its test were ported unchanged as predicted, plus one new test case this
session added to it (a paused sandbox issues no preview query - see
`docs/CANVAS.md`).

Verified end-to-end this session, independent of the patch's own authoring
(a completely separate fresh clone, `git checkout --detach` to
`47eed012171230b059c3f5cbf3798bded74ea248`, `git apply --check` then
`git apply` of this exact patch file): clean apply, `npm ci`, **44/44**
vitest tests across the three files named above, clean `npm run
typecheck`, clean `eslint --fix`, and a real `npm run build` producing a
working `build/index.html` + `build/assets/`. Full detail and the exact
commands are in `docs/CANVAS.md`'s "1.45.0 pin for the DOKS Helm chart"
section.

Also built, run, and curled for real in this worker (Docker is available
here):
[`doks/docker/canvas-pilot.Dockerfile`](../docker/canvas-pilot.Dockerfile)
rebuilds the frontend from this exact pinned commit+patch and layers only
`/opt/agent-canvas/frontend` onto the stock, digest-pinned
`ghcr.io/openhands/agent-canvas:1.45.0` (confirmed by inspection to be
the real per-sandbox all-in-one MARS workspace image - agent-server +
automation + frontend + the `mars-*.mjs` DigitalOcean bridge scripts -
not a separate Enterprise-side web frontend). `docker build` succeeded;
`docker run` showed the real agent-server (`OpenHands SDK v1.63.1`) and
automation service starting; `curl` against the running container's
`/alive` returned the real agent-server's `200`, and against
`/canvas/locales/en/openhands.json` returned the actual served file
containing the new `PAUSE_RUNTIME` key - proof the running container
serves the patched frontend, not just that the build stage produced it
on disk. Still not done: no registry push, no DOKS/live cluster install,
no browser-driven Pause/resume/preview round-trip against a real sandbox
- `agent-canvas.image.repository`/`tag` in `openhands-mars.values.yaml`
therefore still deliberately unset (see that file's own comment).
