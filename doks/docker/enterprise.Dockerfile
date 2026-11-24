# MARS overlay on the released, licensed OpenHands Enterprise image this
# install's `openhands-mars.values.yaml` sets `image.repository`/`tag` to
# (the same release this install's official 0.119.0 chart already pins -
# see doks/docs/HELM.md). Build context: this package's root.
#   docker build -f doks/docker/enterprise.Dockerfile .
# Override only if you intentionally need a different digest:
#   docker build -f doks/docker/enterprise.Dockerfile \
#     --build-arg ENTERPRISE_BASE=<other digest> .
#
# ENTERPRISE_BASE default below is the ghcr.io OCI index digest for
# ghcr.io/openhands/enterprise-server:1.97.1, read directly from the
# registry's own manifest API (GET /v2/openhands/enterprise-server/manifests/1.97.1,
# Accept: application/vnd.oci.image.index.v1+json - the response's
# Docker-Content-Digest header, not derived or guessed), confirming
# amd64 and arm64 linux platform manifests both present under that index.
# Same verification method this package already uses for the 1.88.0
# digest in docs/VERSIONS.md.
ARG ENTERPRISE_BASE=ghcr.io/openhands/enterprise-server:1.97.1@sha256:fdf5df25a89e0fa7187829b41b8b3cd86f5eb99d2420c71dae4dd16e6ae37d07
FROM ${ENTERPRISE_BASE}

# mars_apply_doks.py composes two sources - see its own module docstring
# for the full contract: this package's shared, provider-agnostic
# enterprise/ tree, plus doks/enterprise's 1.97.1-specific hashes/patches/
# migrations. Fails closed: nothing is written unless every stock base
# file matches byte-for-byte and the complete result matches
# doks/enterprise/PATCHED_SHA256.
COPY --chown=openhands:openhands scripts/mars_apply.py /tmp/mars/mars_apply.py
COPY --chown=openhands:openhands scripts/mars_apply_doks.py /tmp/mars/mars_apply_doks.py
COPY --chown=openhands:openhands enterprise /tmp/mars/enterprise
COPY --chown=openhands:openhands doks/enterprise /tmp/mars/doks-enterprise
RUN python3 /tmp/mars/mars_apply_doks.py /tmp/mars/enterprise /tmp/mars/doks-enterprise /app \
 && find /app/openhands/app_server /app/storage /app/server/routes -name '__pycache__' -prune -exec rm -rf {} + \
 && python3 -c "import openhands.app_server.sandbox.do_mars_proxy, openhands.app_server.sandbox.do_mars_sandbox_service, openhands.app_server.sandbox.do_mars_pool_store" \
 && rm -rf /tmp/mars

LABEL org.openhands.mars.package-base="enterprise-server:1.97.1" \
      org.openhands.mars.migration-head="do_mars_doks_002"
# The same image runs the official chart's app/worker (stock CMD, set via
# openhands-mars.values.yaml's image.repository/tag), the migrate-db init
# container (the official chart's own, unmodified - it already runs
# whatever image that Deployment uses), and - with a different command -
# this package's own mars-gateway/mars-pool-reconciler
# (doks/helm/charts/mars-doks). See doks/docs/HELM.md for all three.
