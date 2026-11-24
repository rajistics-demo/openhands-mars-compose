# Build and install the Canvas pilot (Pause control + live preview)

This package does not ship Canvas source or a Canvas image: the stock
`canvas-server` container already serves a built-in bundle, and the pilot
build described here only replaces *that bundle*, not the container image.
The pilot adds two things to stock Canvas 1.35.0, both gated so a site that
never builds or mounts this bundle sees no behavior change:

- A **Pause** control on an Enterprise MARS conversation, calling the
  gateway's authenticated pause route instead of a plain stop. It is shown
  only when the active conversation reports a running sandbox on a backend
  whose `SandboxInfo.supports_runtime_pause` is `true` (see
  `do_mars_sandbox_service.py`; stock Docker/E2B backends are unaffected).
- An embedded, same-origin-cookie-authenticated live preview iframe for a
  recovered conversation opted into it (see "Operator opt-in" below).

The patch is [`canvas/authenticated-preview.patch`](../canvas/authenticated-preview.patch),
tested against Canvas 1.35.0 commit `934612b3b6cee05d3a4400b98b48a9c69215401d`
in the `OpenHands/OpenHands` repository.

## Reproducible build

Requires Node 24 and the exact pinned commit above; no other version has
been tested against this patch.

```sh
git clone --filter=blob:none https://github.com/OpenHands/OpenHands.git canvas-pilot
cd canvas-pilot
git checkout --detach 934612b3b6cee05d3a4400b98b48a9c69215401d

# Inspect before integration: the patch is reviewable text, not a binary.
git apply --check "$MARS_PACKAGE/canvas/authenticated-preview.patch"
git apply "$MARS_PACKAGE/canvas/authenticated-preview.patch"

npm ci
npm run make-i18n
npx vitest run __tests__/components/features/conversation/conversation-name-with-status.test.tsx \
                __tests__/components/features/conversation/pilot-recovered-preview.test.tsx

# VITE_BASE_PATH must match the stock canvas-server --base-path (/canvas).
# VITE_CANVAS_RECOVERY_PILOT_ORIGIN gates the embedded preview to conversations
# opted in on THIS exact app origin (see "Operator opt-in" below) - set it to
# your real HTTPS APP_ORIGIN, the same value rendered into
# AGENT_CANVAS_LOCK_TO_CLOUD / WEB_URL by mars_setup.py.
VITE_BASE_PATH=/canvas VITE_CANVAS_RECOVERY_PILOT_ORIGIN=https://your-app-origin.example npm run build

export CANVAS_PILOT_BUILD="$PWD/build"
```

`$MARS_PACKAGE` is this package's root (the directory containing this file).
Record the build's `git rev-parse HEAD` (must still be the pinned commit)
and the `npm ci`/vitest output for your own evidence; this package only
ships the source patch and the compose wiring below, not a built bundle.

## 1.45.0 pin for the DOKS Helm chart

The Compose recipe above still applies unchanged to the Compose installer.
`doks/helm`'s vendored `openhands` chart (0.119.0) instead pins its
`agent-canvas` subchart image to release **1.45.0** by default (see
`charts/openhands/charts/agent-canvas/values.yaml` in that chart), not
1.35.0, so the patch above does not apply there and needed its own rebase.

[`canvas/authenticated-preview-1.45.0.patch`](../canvas/authenticated-preview-1.45.0.patch)
is that rebase: the same two features (Pause control, gated live preview),
manually re-applied hunk-by-hunk against `OpenHands/OpenHands` commit
`47eed012171230b059c3f5cbf3798bded74ea248` ("chore(main): release 1.45.0
(#20014)") because the 1.35.0 patch's surrounding file context had moved too
far for `git apply` to place it automatically - a straight `git apply` of
the old patch at this commit does not succeed. Reused unchanged from the
1.35.0 patch: `pilot-recovered-preview.tsx` and its test file (both
API-compatible, confirmed by inspection, not just by the patch applying).
Re-adapted for this commit's component: `conversation-name-with-status.tsx`'s
pause button and the `sandbox_supports_runtime_pause` field it reads, the
`routes/conversation.tsx` mount point, and one `COMMON$PAUSE_RUNTIME`
translation key (the 1.35.0 patch's other toast-copy keys were not ported -
this rebase reuses the upstream hook's own existing toast copy unchanged).

Build the same way, against this commit and this patch instead:

```sh
git clone --filter=blob:none https://github.com/OpenHands/OpenHands.git canvas-pilot-1450
cd canvas-pilot-1450
git checkout --detach 47eed012171230b059c3f5cbf3798bded74ea248

git apply --check "$MARS_PACKAGE/canvas/authenticated-preview-1.45.0.patch"
git apply "$MARS_PACKAGE/canvas/authenticated-preview-1.45.0.patch"

npm ci
npm run make-i18n
npx vitest run __tests__/components/features/conversation/conversation-name-with-status.test.tsx \
                __tests__/components/features/conversation/pilot-recovered-preview.test.tsx \
                __tests__/hooks/mutation/use-unified-stop-conversation.test.tsx
npm run typecheck

VITE_BASE_PATH=/canvas VITE_CANVAS_RECOVERY_PILOT_ORIGIN=https://your-app-origin.example npm run build
export CANVAS_PILOT_BUILD="$PWD/build"
```

**What was actually verified this round, on this worker, source-level only
(no container build, no registry push, no live DigitalOcean/DOKS cluster -
none of those are available or permitted from here):**

- A completely independent fresh clone + `git checkout --detach` to the
  pinned commit, then `git apply --check` and `git apply` of this exact
  patch file, both clean.
- `npm ci` (1316 packages), `npm run make-i18n`, and the three test files
  above: **44 of 44 tests passed**, including a new
  `"does not query, and renders nothing, for a paused sandbox"` case added
  this round (`pilot-recovered-preview.test.tsx`) asserting the preview's
  `useQuery` is called with `enabled: false` whenever
  `sandbox_status !== "RUNNING"` - i.e. mounting this component for a
  paused conversation issues no request and cannot itself wake the sandbox
  - and the existing cross-origin/cross-sandbox/unauthenticated-preview
  rejection cases (`it.each` of mismatched sandbox IDs/ports/origins, and
  the 401-refused-authorization case) kept passing unchanged.
- `npm run typecheck` (`react-router typegen && tsc`): clean.
- `npx eslint --fix` on every changed file: clean except one pre-existing,
  unrelated warning (`ml-[3.5px]` arbitrary Tailwind value on an untouched
  line).
- `VITE_BASE_PATH=/canvas VITE_CANVAS_RECOVERY_PILOT_ORIGIN=... npm run build`
  completed and produced a real `build/index.html` plus `build/assets/`
  (the same flat layout `CANVAS_PILOT_BUILD` already expects).

**Explicitly NOT verified this round** (same gaps as the 1.35.0 pilot's own
validation notes above, now also true for this rebase): no browser-driven
Pause/resume/preview round-trip against a live sandbox, no DOKS/Helm
install with it mounted, no live DigitalOcean MARS VM. Do not describe
any of the above as "live-tested" - only the source-level build, lint,
typecheck, unit-test, and (below) local Docker evidence applies to this
pin so far.

### Packaged as a Docker image

[`doks/docker/canvas-pilot.Dockerfile`](../doks/docker/canvas-pilot.Dockerfile)
automates the recipe above into a reproducible, multi-stage image: a
`node:24-bookworm` stage fetches the pinned commit, applies this exact
patch file, and runs `npm ci && npm run build`; the final stage starts
`FROM` the stock, digest-pinned `ghcr.io/openhands/agent-canvas:1.45.0`
and replaces only `/opt/agent-canvas/frontend` with the rebuilt bundle -
everything else in that image (the real agent-server, the automation
service, the `mars-*.mjs` DigitalOcean bridge scripts, `entrypoint.sh`)
is the stock, unmodified image content. `AGENT_CANVAS_BASE`/`NODE_BASE`
are pinned to OCI index digests read directly from `ghcr.io`/Docker Hub's
own registry APIs in this worker (`docker-content-digest` response
header), not guessed; `docker pull`ing the agent-canvas reference
confirmed the local digest matches exactly.

```bash
docker build -f doks/docker/canvas-pilot.Dockerfile \
  --build-arg VITE_CANVAS_RECOVERY_PILOT_ORIGIN=https://<your-app-origin> \
  -t <your-registry>/agent-canvas-mars-pilot:1.45.0 .
```

Point `openhands-mars.values.yaml`'s `agent-canvas.image.repository`/
`tag` at the pushed result to roll this out; leave them unset (this
package's shipped default) to keep the stock, un-patched image.

**Verified this round, for real, in this worker** (Docker available
locally - the one piece of the "explicitly not verified" list above that
this environment actually permits): `docker build` of this exact
Dockerfile completed successfully; the resulting image was run with
`docker run -p <port>:8000 ...` and its real startup log showed the
actual agent-server (`OpenHands SDK v1.63.1`), the automation service,
and the static-server/proxy all starting; `curl` against the running
container's own `/alive` returned the real agent-server's `200`, and
`curl` against `/canvas/locales/en/openhands.json` returned the real
served file containing the new `PAUSE_RUNTIME` key - confirming the
patched frontend is what the built container actually serves over HTTP,
not just what the build stage produced on disk. **Still not done:** no
registry push, no DOKS/Helm install using this image, no browser-driven
Pause/resume/preview click-through against it, no live DigitalOcean MARS
VM - all of those remain exactly as listed above.

## Install the build

### Standard route: `mars_setup.py check`/`up`

Set `CANVAS_PILOT_BUILD=<absolute path to the build directory from above>`
in `site.env`, alongside your other settings. `check --stage mars` and
`up --mars` then do the rest automatically:

- `Install.config_files` layers
  [`compose/docker-compose.canvas-pilot.yml`](../compose/docker-compose.canvas-pilot.yml)
  **last**, after `docker-compose.yml`, `docker-compose.mars.yml` and
  `docker-compose.site.yml`, with the same `--env-file` list already
  rendered under `private/` - no manual `-f`/`--env-file` list to keep in
  sync by hand.
- `validate_site` rejects a `CANVAS_PILOT_BUILD` that is not an absolute
  path, not a directory, or has no readable `index.html` (i.e. not an
  `npm run build` output) **before** `check` ever shells out to Compose.
- `verify_rendered` then asserts, on the actual merged `compose config`
  output, both that `canvas-server` bind-mounts exactly that path read-only
  at `/opt/agent-canvas/frontend` and the existing `--lock-to-cloud` check
  (`WEB_URL`/`APP_ORIGIN`) still holds with this overlay layered in.

`CANVAS_PILOT_BUILD` is a local build directory, not a pinned registry
image, so this is as far as the installer's own image-digest/checksum
pinning (`mars_release.py`, `mars_apply.py`) can verify it automatically -
building the bundle itself (above) stays your own, separately-evidenced
step. Leave `CANVAS_PILOT_BUILD` unset in `site.env` and every other
install is byte-for-byte what it was before this setting existed.

### Manual route (no site.env tracking)

Operators who do not want `CANVAS_PILOT_BUILD` recorded in `site.env` can
still layer the file by hand, identically to the standard route internally:

```sh
export CANVAS_PILOT_BUILD="$PWD/build"   # from the build step above
docker compose -p <project> --project-directory <install-dir> \
  -f docker-compose.yml -f compose/docker-compose.mars.yml \
  -f installer/compose/docker-compose.site.yml \
  -f compose/docker-compose.canvas-pilot.yml \
  --env-file <install-dir>/private/stack.env \
  --env-file <install-dir>/private/compose.env \
  config --quiet
```

A non-zero exit, or output, means something above does not resolve; fix it
before `up`. A clean exit confirms the merge is valid and `CANVAS_PILOT_BUILD`
and `WEB_URL` both resolve - `WEB_URL` is already rendered into
`private/compose.env` by `mars_setup.py check`/`render-env-spec`. `mars_setup.py
check --stage mars` only runs the `--lock-to-cloud`/bind-mount assertions
above when `CANVAS_PILOT_BUILD` is set in `site.env`; on the manual route it
is your own responsibility to re-run an equivalent check after any change.

Do not route a public hostname at the rebuilt `canvas-server` until the
`config --quiet` check (standard route: `mars_setup.py check --stage mars`)
above is clean. A bundle built with the wrong
`VITE_CANVAS_RECOVERY_PILOT_ORIGIN`, or layered without `--lock-to-cloud`
resolving to your real `APP_ORIGIN`, fails closed (no preview, no Pause
button) rather than serving the stock or wrong behavior - see
`PilotRecoveredPreview`'s origin check in the patch - but confirming the
merge first avoids chasing that in the browser.

## Operator opt-in per conversation

The embedded preview is further gated per-conversation, independent of the
Pause control. Set `DO_MARS_ENABLE_BROWSER_PREVIEW=true` (see
"Enable Canvas previews" in [INSTALL.md](INSTALL.md)) and, on the owning
organization, tag the specific conversation:

```sh
curl -X PATCH "$APP_ORIGIN/api/v1/app-conversations/<conversation-id>" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"tags": {"canvasrecoverypilotpreview": "true"}}'
```

Conversations without this tag only ever get the gateway's resume-and-reconnect
behavior; nothing about this package's non-pilot sandbox lifecycle changes.

## Validation

The two focused Canvas tests and the pinned production bundle build ran
on AWS. The standard installer served that bundle on the isolated
Enterprise Compose stack with live MARS.

The browser test confirmed that Pause kept the original VM paused even
when the preview was requested. Reopening the same conversation returned
its stored history and reconnected to the original preview process with
its in-memory counter intact. [Measured replay](STATUS.md#live-mars-warm-pool-and-canvas-replay)
