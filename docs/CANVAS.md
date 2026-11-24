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

## What is verified where

- The gateway-side preview session exchange, cookie scoping, origin checks,
  and the paused-sandbox 409 (settled pause is never woken by a preview
  request) are exercised against real behavior in
  `enterprise/tests/unit/app_server/test_do_mars_browser_preview.py`.
- The frontend's origin/port/path matching and credentialed-session gating
  are exercised by the two vitest files named in the build recipe above,
  which are not runnable in this environment (no Canvas source tree here);
  run them during the build step and keep that output as your own evidence.
- `compose/docker-compose.canvas-pilot.yml` was validated in this
  environment with real `docker compose config` against a synthetic stub
  `canvas-server` service standing in for the stock one (which this package
  does not ship) - this proves the override's YAML, the `--lock-to-cloud`
  substitution and the bind-mount resolve exactly as written, not that the
  stock image's actual CLI accepts these exact flags end-to-end.
