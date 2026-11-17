#!/usr/bin/env bash
# Read-only preflight for the MARS evaluation overlay. Changes nothing.
# Usage: scripts/preflight.sh <stock docker-compose.yml> <mars.env>
# Exit 0 only if every check passes.
set -u
PKG=$(cd "$(dirname "$0")/.." && pwd)
COMPOSE_FILE=${1:?usage: preflight.sh <docker-compose.yml> <mars.env>}
ENV_FILE=${2:?usage: preflight.sh <docker-compose.yml> <mars.env>}
ENT_BASE=ghcr.io/openhands/enterprise-server:1.88.0@sha256:cc67d10163e9e466b65d7a6e765a020fcc23ca036e78c6a6d846c458d1e2d9eb
AUT_BASE=ghcr.io/openhands/automation:1.30.0@sha256:fb722b6fcde1a7fa67a15c40eb83af0ca23f1c111d0bc745f3b77a8b1bd33afa
fail=0
ok() { printf 'ok    %s\n' "$*"; }
bad() { printf 'FAIL  %s\n' "$*"; fail=1; }

(cd "$PKG" && sha256sum --quiet -c SHA256SUMS) && ok "package files match SHA256SUMS" || bad "package SHA256SUMS mismatch"

ver=$(docker compose version --short 2>/dev/null | sed 's/^v//')
if [ -n "$ver" ] && [ "$(printf '%s\n2.24.4\n' "$ver" | sort -V | head -1)" = "2.24.4" ]; then
  ok "docker compose $ver (>= 2.24.4 for !override)"
else
  bad "docker compose >= 2.24.4 required (found '${ver:-none}')"
fi

# shellcheck source=/dev/null
set -a; . "$ENV_FILE"; set +a
for v in MARS_APP_IMAGE MARS_AUTOMATION_IMAGE DO_MARS_CONFIG_ID DO_MARS_GATEWAY_URL DO_MARS_CALLBACK_URL \
         DO_MARS_APP_ORIGIN AUTOMATION_SANDBOX_OPENHANDS_API_URL MARS_SECRETS_DIR; do
  [ -n "${!v:-}" ] && ok "$v set" || bad "$v missing"
done
for v in DO_MARS_GATEWAY_URL DO_MARS_CALLBACK_URL DO_MARS_APP_ORIGIN AUTOMATION_SANDBOX_OPENHANDS_API_URL; do
  case "${!v:-}" in https://*) ok "$v is https";; *) bad "$v must be https";; esac
done
gw_host=$(printf '%s' "${DO_MARS_GATEWAY_URL:-}" | sed -E 's#https://([^/:]+).*#\1#')
app_host=$(printf '%s' "${DO_MARS_APP_ORIGIN:-}" | sed -E 's#https://([^/:]+).*#\1#')
[ -n "$gw_host" ] && [ "$gw_host" != "$app_host" ] && ok "runtime origin separate from app origin" \
  || bad "DO_MARS_GATEWAY_URL must use a hostname different from the app origin"
ttl=${AUTOMATION_RUN_KEY_TTL_SECONDS:-3600}
[ "$ttl" -ge 0 ] 2>/dev/null && [ "$ttl" -le 86400 ] && ok "AUTOMATION_RUN_KEY_TTL_SECONDS=$ttl" || bad "run key TTL must be 0..86400"

for f in do_team_token session_signing_key init_api_key; do
  p="${MARS_SECRETS_DIR:-/nonexistent}/$f"
  if [ -s "$p" ]; then
    mode=$(stat -c '%a' "$p")
    case "$mode" in *[1-7]) bad "$p is world-accessible (mode $mode)";; *) ok "$p present (mode $mode)";; esac
  else
    bad "$p missing or empty"
  fi
done
if [ -s "${MARS_SECRETS_DIR:-/nonexistent}/session_signing_key" ]; then
  len=$(tr -d '\n' < "$MARS_SECRETS_DIR/session_signing_key" | wc -c)
  [ "$len" -ge 32 ] && ok "signing key length >= 32" || bad "signing key shorter than 32 bytes"
fi

for img in "$ENT_BASE" "$AUT_BASE"; do
  docker image inspect "$img" >/dev/null 2>&1 && ok "base image present: ${img%@*}" \
    || bad "base image not pulled: $img (needs licensed registry access)"
done
if docker image inspect "$ENT_BASE" >/dev/null 2>&1; then
  docker run --rm --network none --entrypoint python3 -v "$PKG:/pkg:ro" "$ENT_BASE" \
    /pkg/scripts/mars_apply.py /pkg/enterprise /app --check && ok "enterprise base accepts overlay" \
    || bad "enterprise base rejects overlay"
fi
if docker image inspect "$AUT_BASE" >/dev/null 2>&1; then
  docker run --rm --network none --entrypoint sh -v "$PKG:/pkg:ro" "$AUT_BASE" -c \
    'site=$(/opt/venv/bin/python -c "import openhands.automation as m, pathlib; print(pathlib.Path(m.__file__).resolve().parents[2])"); /opt/venv/bin/python /pkg/scripts/mars_apply.py /pkg/automation "$site" --check' \
    && ok "automation base accepts patches" || bad "automation base rejects patches"
fi

# Database: the package migration must sit on 213 (fresh install) or do_mars_001 (re-install).
rev=$(docker compose -f "$COMPOSE_FILE" exec -T db psql -U "${DB_USER:-openhands}" -d "${DB_NAME:-openhands}" \
      -tAc 'select version_num from alembic_version' 2>/dev/null | tr -d '[:space:]')
case "$rev" in
  213) ok "database at 213 (stock 1.88.0); do_mars_001 will be applied";;
  do_mars_001) ok "database already at do_mars_001";;
  "") bad "could not read alembic_version (is the db service running?)";;
  *) bad "database at '$rev': this package supports only 213 or do_mars_001";;
esac

grep -q '/var/run/docker.sock' "$COMPOSE_FILE" && echo "note  base file mounts the Docker socket; the overlay removes it from app/worker"
[ $fail -eq 0 ] && echo "PREFLIGHT PASSED" || echo "PREFLIGHT FAILED"
exit $fail
