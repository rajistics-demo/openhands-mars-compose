# MARS patches on the released OpenHands Automation 1.30.0 image (MIT).
# Build context: the package root.  docker build -f docker/automation.Dockerfile .
# The stock image installs the service non-editable into /opt/venv, so the
# patches target that site-packages tree after a byte-exact base check.
ARG AUTOMATION_BASE=ghcr.io/openhands/automation:1.30.0@sha256:fb722b6fcde1a7fa67a15c40eb83af0ca23f1c111d0bc745f3b77a8b1bd33afa
FROM ${AUTOMATION_BASE}

USER root
COPY scripts/mars_apply.py /tmp/mars/mars_apply.py
COPY automation /tmp/mars/automation
RUN set -eu; \
    site=$(/opt/venv/bin/python -c "import openhands.automation as m, pathlib; print(pathlib.Path(m.__file__).resolve().parents[2])"); \
    /opt/venv/bin/python /tmp/mars/mars_apply.py /tmp/mars/automation "$site"; \
    find "$site/openhands/automation" -name '__pycache__' -prune -exec rm -rf {} +; \
    /opt/venv/bin/python -c "import openhands.automation.router, openhands.automation.backends.cloud"; \
    rm -rf /tmp/mars
USER automation

LABEL org.openhands.mars.package-base="automation:1.30.0"
