# Opt-in overlay image: rebuilds the agent-canvas frontend from pinned
# upstream source with this package's Canvas Pause + authenticated
# live-preview pilot patch applied, then copies only the rebuilt
# `frontend/` static bundle onto the stock, unmodified all-in-one
# agent-canvas:1.45.0 image (Agent Server + Automation + frontend - this
# is the per-sandbox MARS workspace image, not a separate Enterprise web
# frontend; confirmed by inspecting the stock image's own
# AGENT_CANVAS_BUILD_GIT_SHA env var and /opt/agent-canvas layout).
# Nothing else in that image - the real agent-server, automation service,
# mars-*.mjs DigitalOcean bridge scripts, entrypoint.sh - is touched.
#
# Build (from this package's root):
#   docker build -f doks/docker/canvas-pilot.Dockerfile \
#     -t <your-registry>/agent-canvas-mars-pilot:1.45.0 .
#
# Point `openhands-mars.values.yaml`'s `agent-canvas.image.repository`/
# `tag` at the result to roll it out; leave them unset (this package's
# default) to keep running the stock, un-patched image instead.
#
# FRONTEND_BASE pins the exact OpenHands/OpenHands commit this image's
# own AGENT_CANVAS_BUILD_GIT_SHA/org.opencontainers.image.revision report
# building agent-canvas:1.45.0 from (read directly off the pulled image's
# own `docker inspect` output in this evaluation worker, not guessed) -
# the same commit `canvas/authenticated-preview-1.45.0.patch` targets.
# NODE_BASE is `node:24-bookworm`'s OCI index digest, read from Docker
# Hub's own registry API (`Docker-Content-Digest` response header) in
# this worker, the same verification method this package's other
# pinned-digest comments already use (see docs/VERSIONS.md).
ARG FRONTEND_BASE=https://github.com/OpenHands/OpenHands.git
ARG FRONTEND_COMMIT=47eed012171230b059c3f5cbf3798bded74ea248
ARG NODE_BASE=node:24-bookworm@sha256:6b33b0be1647d1a577f9cebb6c8fc2587cc02c98c9956af8d0289df1791a3d10
ARG AGENT_CANVAS_BASE=ghcr.io/openhands/agent-canvas:1.45.0@sha256:4eb6d6fec924573783d424ee7b911b01eb1bd620cd8bb6e69fbc6b215599083a

FROM ${NODE_BASE} AS frontend-build
ARG FRONTEND_BASE
ARG FRONTEND_COMMIT
WORKDIR /src

# Fetch the exact pinned commit only (not a branch/tag HEAD that could
# move), the same "fails closed on drift" spirit as mars_apply_doks.py's
# byte-for-byte base check - `git fetch <sha>` errors out if the upstream
# repo ever stops serving that object instead of silently building
# something else.
RUN git init -q \
 && git remote add origin "${FRONTEND_BASE}" \
 && git fetch --depth 1 origin "${FRONTEND_COMMIT}" \
 && git checkout -q FETCH_HEAD

COPY canvas/authenticated-preview-1.45.0.patch /tmp/authenticated-preview-1.45.0.patch
RUN git apply --check /tmp/authenticated-preview-1.45.0.patch \
 && git apply /tmp/authenticated-preview-1.45.0.patch \
 && rm /tmp/authenticated-preview-1.45.0.patch

# VITE_BASE_PATH matches the stock image's own AGENT_CANVAS_BASE_PATH
# default so built asset URLs resolve the same way under the chart's
# ingress. VITE_CANVAS_RECOVERY_PILOT_ORIGIN is deliberately left for the
# operator to pass with --build-arg (it is per-install, the real
# installer-managed WEB_URL/APP_ORIGIN - see docs/CANVAS.md) rather than
# defaulted here to a placeholder that could ship unnoticed.
ARG VITE_BASE_PATH=/canvas
ARG VITE_CANVAS_RECOVERY_PILOT_ORIGIN=""
RUN npm ci \
 && VITE_BASE_PATH="${VITE_BASE_PATH}" \
    VITE_CANVAS_RECOVERY_PILOT_ORIGIN="${VITE_CANVAS_RECOVERY_PILOT_ORIGIN}" \
    npm run build

FROM ${AGENT_CANVAS_BASE}
ARG FRONTEND_COMMIT
USER root
RUN rm -rf /opt/agent-canvas/frontend
COPY --from=frontend-build --chown=root:root /src/build /opt/agent-canvas/frontend
USER openhands
LABEL org.openhands.mars.canvas-pilot-source-commit="${FRONTEND_COMMIT}" \
      org.openhands.mars.canvas-pilot-patch="canvas/authenticated-preview-1.45.0.patch"
