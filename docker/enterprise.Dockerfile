# MARS overlay on the released, licensed OpenHands Enterprise 1.88.0 image.
# Build context: the package root.  docker build -f docker/enterprise.Dockerfile .
# Pulling the base needs registry access granted with your Enterprise licence.
# The digest was read from ghcr.io on 2026-11-17 (see docs/VERSIONS.md); the
# build refuses any base whose patched files differ from the 1.88.0 release.
ARG ENTERPRISE_BASE=ghcr.io/openhands/enterprise-server:1.88.0@sha256:cc67d10163e9e466b65d7a6e765a020fcc23ca036e78c6a6d846c458d1e2d9eb
FROM ${ENTERPRISE_BASE}

COPY --chown=openhands:openhands scripts/mars_apply.py /tmp/mars/mars_apply.py
COPY --chown=openhands:openhands enterprise /tmp/mars/enterprise
RUN python3 /tmp/mars/mars_apply.py /tmp/mars/enterprise /app \
 && find /app/openhands/app_server /app/storage /app/server/routes -name '__pycache__' -prune -exec rm -rf {} + \
 && python3 -c "import openhands.app_server.sandbox.do_mars_proxy, openhands.app_server.sandbox.do_mars_sandbox_service" \
 && rm -rf /tmp/mars

LABEL org.openhands.mars.package-base="enterprise-server:1.88.0" \
      org.openhands.mars.migration-head="do_mars_002"
# The same image runs the app (stock CMD) and, with a different command, the
# runtime gateway; see compose/docker-compose.mars.yml.
