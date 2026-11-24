# DOKS Helm install: official `openhands` chart + `mars-doks` companion chart

Two independent Helm releases make up a MARS-enabled DOKS install:

1. **`openhands`** - the official chart, `oci://ghcr.io/openhands/helm-charts/openhands`,
   version `0.119.0` (`appVersion` `1.97.1`). This renders the app/worker
   Deployment (`openhands`), Automation (`automation`), Canvas
   (`agent-canvas`), and every other resource that chart already owns -
   runtime-api, integrations, MCP, ingresses, cron jobs, and (per this
   install's own existing choices) PostgreSQL/Keycloak/Redis/Valkey/
   object storage/LiteLLM.
2. **`mars-doks`** - this package's own small companion chart
   (`helm/charts/mars-doks`). It renders only what the official chart has
   no value for: the authenticated runtime gateway (`mars-gateway`), the
   opt-in warm-pool reconciler (`mars-pool-reconciler`), and the Canvas
   preview-domain Ingress.

Use the existing release name, namespace, and site values for OpenHands.
Install the companion chart in the same namespace. Run the commands below
from the repository root. The post-renderer needs Python 3 and PyYAML on
the machine running Helm. Keep it in the Helm invocation for subsequent
upgrades while MARS is enabled.

Build and push the [Enterprise image](../docker/enterprise.Dockerfile)
to a registry your cluster can pull from:

```sh
docker build -f doks/docker/enterprise.Dockerfile -t "$ENTERPRISE_IMAGE" .
docker push "$ENTERPRISE_IMAGE"
```

Set `ENTERPRISE_IMAGE` to a fully qualified registry name and tag before
running those commands. For the optional Pause and preview UI, also
[build and push the Canvas image](../../docs/CANVAS.md#packaged-as-a-docker-image).

## Why a post-renderer, and why only for one Deployment

The official chart's `openhands` Deployment
(`charts/openhands/templates/deployment.yaml`) takes `env` as a plain
`{name, value}` list (`templates/_env.yaml`'s `openhands.env` helper) and
has no `extraVolumes`/`extraVolumeMounts` value. `DO_MARS_TEAM_TOKEN_FILE`
etc. are themselves just path strings set through `env`; only the volume
that puts a file at that path needs a change this chart's own values
cannot express. `automation` and `agent-canvas` do not read those files
and are left untouched - confirmed by inspecting the rendered manifest
(see "Render-proof" below): after the post-renderer runs, the
`mars-secrets` volume/mount appears on `Deployment/openhands` only.

This is the Helm-documented post-rendering mechanism
(https://helm.sh/docs/topics/advanced/#post-rendering): a script that
reads the whole rendered multi-document manifest on stdin and writes the
modified stream to stdout. `mount_mars_secrets.py` fails closed if it
finds zero or more than one Deployment named `openhands` in that stream,
rather than silently mounting the secret nowhere or in the wrong place.

## Scope rule for `openhands-mars.values.yaml`

`helm/values/openhands-mars.values.yaml` adds only what MARS itself
requires: `image.repository`/`tag` (the MARS-patched Enterprise image),
`env.DO_MARS_*` (provider config), and `agent-canvas.image.*` +
`agent-canvas.staticServer.lockToCloud` (the Canvas pilot build). It
never sets `postgresql`/`keycloak`/`redis`/`valkey`/`minio`/
`litellm-helm`, `ingress.*`, `externalDatabase.*`, or `automation.*`
(other than nothing - Automation in 1.97.1 dispatches runs against
whatever `OH_SANDBOX_KIND` the app is configured with and needs no
MARS-specific values of its own). Those all stay exactly what this
install's existing values already set them to.

### `DO_MARS_GATEWAY_URL` vs `DO_MARS_INTERNAL_URL`

Both are real fields on `DoMarsSandboxServiceInjector`
(`enterprise/overlay/openhands/app_server/sandbox/do_mars_sandbox_service.py`):

- `DO_MARS_GATEWAY_URL` -> `gateway_base_url` (**required**). Used to
  build every sandbox proxy URL returned **to the browser**
  (`f'{self.gateway_base_url...}/{row.id}/8000'`). Must be the gateway's
  real public HTTPS origin (the hostname `helm/charts/mars-doks`'s own
  gateway Ingress routes) - never a cluster-internal Service DNS name or
  ClusterIP.
- `DO_MARS_INTERNAL_URL` -> `internal_url` (optional). Used only for this
  Deployment's own server-side control-plane calls to the gateway (health
  probes, pause/resume dispatch), so they can stay in-cluster via the
  gateway Service's ClusterIP DNS name instead of bouncing through the
  public Ingress. Never returned to a browser client.

### `DO_MARS_APP_ORIGIN` / `web_url` - a gap this file cannot close for you

The gateway process (`do_mars_proxy.py`) reads its own cookie-domain input
from `DO_MARS_APP_ORIGIN` (a plain env var). The app/worker side
(`do_mars_sandbox_service.py`) instead derives the equivalent value from
`get_global_config().web_url` - a runtime-resolved Enterprise setting,
**not** a static env var this chart exposes as a single values key. The
two must be byte-identical for `preview_origin`/cookie-domain matching to
work. This values file cannot verify that match for you; confirm it
against this install's actual running config (e.g. its own
`/server_info`/admin config endpoint) after upgrading, rather than
assuming the `<REQUIRED:...>` placeholder you filled in is correct.

### Application, gateway and callback addresses

Keep the browser application origin consistent across Enterprise,
Canvas's `staticServer.lockToCloud`, the Canvas build argument
`VITE_CANVAS_RECOVERY_PILOT_ORIGIN`, and the companion chart's `webUrl`.
Use a separate hostname for `DO_MARS_GATEWAY_URL` and match it to the
companion chart's `gatewayUrl`. Use the Enterprise origin as the
`DO_MARS_CALLBACK_URL` in the public-ingress setup below. See
[callback networking](CALLBACKS.md) for private DOKS.

## Install with site values

Create two site values files outside the repository. Replace the sample
entries with your values and retain the existing OpenHands release's
other settings.

`openhands-site-mars.yaml`, layered after the supplied OpenHands MARS overlay:

```yaml
image:
  repository: registry.example.com/openhands-enterprise-mars
  tag: 1.97.1-mars-doks
env:
  DO_MARS_CONFIG_ID: "<your MARS configuration ID>"
  DO_MARS_GATEWAY_URL: "https://<your-gateway-hostname>"
  DO_MARS_INTERNAL_URL: "http://<mars-doks-release-name>-mars-gateway.<namespace>.svc.cluster.local:8100"
  DO_MARS_CALLBACK_URL: "https://<your-app-hostname>"
  DO_MARS_APP_ORIGIN: "https://<your-app-hostname>"
agent-canvas:
  staticServer:
    lockToCloud: "https://<your-app-hostname>"
```

`mars-doks-site.yaml`, for the companion chart:

```yaml
image:
  repository: registry.example.com/openhands-enterprise-mars
  tag: 1.97.1-mars-doks
webUrl: "https://<your-app-hostname>"
gatewayUrl: "https://<your-gateway-hostname>"   # same origin as DO_MARS_GATEWAY_URL above; always required even when ingress.gateway.host is also set
secrets:
  existingSecret: mars-secrets   # must match the Secret name created below
database:
  host: "<your-postgresql-host>"              # same instance this install's own externalDatabase.host already points at
  existingSecret: "<your-existing-DB_USER/DB_PASS-secret>"  # a value only this chart reads, not shared with the official chart's own DB wiring - see the note below
doMars:
  configId: "<your DO_MARS_CONFIG_ID>"
ingress:
  className: "<your-ingress-class>"
  gateway:
    host: "<your-gateway-hostname>"
    tlsSecretName: "<your-gateway-tls-secret>"
```

The `mars-secrets` Secret itself (team token, session-signing key, and
init API key the gateway/pool-reconciler/app all read from - see
`do_mars_credentials.py` and the three `DO_MARS_*_FILE` env vars) is
**not** a Helm value anywhere: create it once, out of band, with
`kubectl` (or your own secrets tooling), with all three keys the
companion chart's `secretVolume` helper and `mount_mars_secrets.py` both
require - then reference only its *name* from `secrets.existingSecret`
(companion chart) and `MARS_SECRET_NAME` (post-render) and nowhere else:

```bash
kubectl -n "$NAMESPACE" create secret generic mars-secrets \
  --from-file=mars_team_token=/path/to/team-token \
  --from-file=mars_session_signing_key=/path/to/signing-key \
  --from-file=mars_init_api_key=/path/to/init-key
```

```bash
export MARS_SECRET_NAME=mars-secrets   # must match the Secret name just created
helm upgrade "$OPENHANDS_RELEASE" oci://ghcr.io/openhands/helm-charts/openhands \
  -n "$NAMESPACE" --version 0.119.0 --wait \
  -f existing-openhands.values.yaml \
  -f doks/helm/values/openhands-mars.values.yaml \
  -f openhands-site-mars.yaml \
  --post-renderer doks/helm/postrender/mount_mars_secrets.py

helm upgrade --install mars-doks ./doks/helm/charts/mars-doks \
  -n "$NAMESPACE" --wait -f mars-doks-site.yaml
```

Set `OPENHANDS_RELEASE` and `NAMESPACE` to the existing release and
namespace. Start the trial with the warm pool disabled. Back up
PostgreSQL first. The patched Enterprise image's migration adds provider
state on top of stock 1.97.1. An existing experimental MARS Compose
database uses a different migration lineage.

For the optional Canvas features, set `agent-canvas.image.repository`
and `agent-canvas.image.tag` in `openhands-site-mars.yaml` to the patched
image. Set `env.DO_MARS_ENABLE_BROWSER_PREVIEW: "true"` in that file
and `doMars.enableBrowserPreview: true` in `mars-doks-site.yaml`. Route
the wildcard preview hostname as described in [callback networking](CALLBACKS.md).
To enable the warm pool, match the config ID, Sandbox Spec ID, and
inventory limits in the OpenHands overlay and companion values. Keep
the pool connection direct or session-pooled so PostgreSQL advisory
locks stay on one backend connection.

**A note on `database.existingSecret` above, since it is easy to assume
otherwise:** this companion chart's `database.*` values are read only by
`mars-doks`'s own Deployments (`mars-gateway`/`mars-pool-reconciler`,
which this package adds) and are entirely separate from the official
chart's own database plumbing - setting them does not change, and is not
shared with, anything in your `openhands` release's values. Reading the
official chart's own `templates/_env.yaml` directly: its main app's
`DB_PASS` always comes from `postgresql.auth.existingSecret` (key
`password`), **not** from the top-level `externalDatabase.existingSecret`
values key, even with `postgresql.enabled: false` and
`externalDatabase.enabled: true` - the latter key only supplies
`DB_HOST`/`DB_PORT`/`DB_USER`/`DB_NAME` as plain values, never the
password. If this install already uses an external database, its
`postgresql.auth.existingSecret` (not `externalDatabase.existingSecret`)
is the value actually holding the password Secret name - point this
chart's own `database.existingSecret` at a Secret with `DB_USER`/`DB_PASS`
keys (this chart's own contract, not the official chart's `password`-only
key), which may be a new Secret or a copy of the existing one under those
key names.

## Canvas bundle preflight after upgrade

Run this once per `helm upgrade` that touches `agent-canvas.staticServer.lockToCloud`,
against the real install, before trusting the Pause/live-preview pilot (if
built - see `docs/CANVAS.md`) or stock Canvas's own backend-locking on this
origin. This is a manual operator check, not a Kubernetes probe this
package adds or changes - see "Why not a cluster-wired probe" below for
why it stops there.

**The gap this closes:** `agent-canvas`'s own chart ships only a stock,
content-blind `readinessProbe`/`livenessProbe` -
`httpGet: {path: /, port: http}` (`charts/openhands/charts/agent-canvas/templates/deployment.yaml`,
confirmed by reading that template; unrelated to the MARS patch and
unchanged by it). That probe passes as soon as the static server answers
`GET /` with any 2xx body - it has no idea what origin
`staticServer.lockToCloud` actually rendered into the page. A copy/paste
error in `site.values.yaml` (wrong hostname, stray scheme, trailing path)
still serves a `200 OK` from a perfectly "Ready" pod, so Kubernetes never
reports anything wrong; what breaks is the frontend's own
`getLockedCloudHost()` (`src/api/agent-server-config.ts` in the
`agent-canvas` source, confirmed by inspection) silently locking users to
the wrong backend origin, or - once the 1.45.0 pilot bundle from
`docs/CANVAS.md` is built - its `VITE_CANVAS_RECOVERY_PILOT_ORIGIN`
same-origin check failing closed with no visible error at all.

**The check:** the static server (`scripts/static-server.mjs` in the
`agent-canvas` source, confirmed by inspection) injects
`lockToCloud` into every served page as a literal, unauthenticated
inline script - `window.__AGENT_CANVAS_LOCK_TO_CLOUD__="<value>";` - so
the exact same unauthenticated `GET /` the stock probe already allows is
enough to confirm the *value*, not just the *status code*:

```bash
# Run after `helm upgrade openhands ...` with this chart's
# agent-canvas.staticServer.lockToCloud set, from any host that can reach
# the public ingress (no cluster access or credentials required - this is
# the same unauthenticated path the stock readinessProbe already hits).
expected_origin="https://<your-app-hostname>"   # same value as lockToCloud / DO_MARS_APP_ORIGIN above
actual_canvas_origin="https://<your-canvas-hostname>/canvas/"   # this install's real agent-canvas ingress + VITE_BASE_PATH

page="$(curl -fsS "$actual_canvas_origin")" || { echo "FAIL: agent-canvas did not return 200 from $actual_canvas_origin"; exit 1; }
printf '%s\n' "$page" | grep -F "__AGENT_CANVAS_LOCK_TO_CLOUD__=\"${expected_origin}\"" >/dev/null \
  && echo "OK: lockToCloud serves ${expected_origin}" \
  || { echo "FAIL: served page's __AGENT_CANVAS_LOCK_TO_CLOUD__ does not match ${expected_origin} - check agent-canvas.staticServer.lockToCloud in site.values.yaml"; exit 1; }
```

A `FAIL` here means the pod is `Ready` by Kubernetes' own stock probe
while still serving a backend-lock the operator did not intend - exactly
the silent-misconfiguration case the stock probe cannot catch.

Verified this round: the exact command above, run against the real
`static-server.mjs --lock-to-cloud https://expected.example.com` on this
worker (not a live DOKS cluster) - the match case prints `OK`, and a
deliberately wrong `expected_origin` correctly prints `FAIL` instead of
false-passing.

### Why not a cluster-wired probe

A real fix would replace `agent-canvas`'s stock `readinessProbe` with one
that greps for the expected origin, e.g. an `exec` probe running the same
`curl | grep` above inside the pod. That means patching a template in the
*vendored, unmodified `openhands` chart* (not this repo's own
`mars-doks` chart), which is out of this task's scope ("no changes outside
this repo") and was not attempted. Until an operator forks that template
(or the upstream chart adopts one), this preflight command is the
mitigation: run manually, once, right after every upgrade that changes
`lockToCloud` - not continuously, and not enforced by Kubernetes itself.

## Render-proof

Evidence this package's own claims rest on (raw render output kept local
to the evaluation worker only, per this task's no-publish constraint - not
included in the patch):

```bash
helm template openhands /path/to/openhands-0.119.0.tgz \
  -f helm/values/render-fixture.values.yaml \
  -f helm/values/openhands-mars.values.yaml \
  -f <test fill-in for the <REQUIRED:...> placeholders> \
  | MARS_SECRET_NAME=mars-secrets python3 helm/postrender/mount_mars_secrets.py \
  > /tmp/official-render-patched.yaml
```

Verified against the published chart tarball
(sha256 `7902d6528041e6227767f13ed48faafcaa9d6644ab915e2c1f0597dacf7ab6f7`):

- Exactly one `Deployment/openhands`, one `Deployment/automation`, one
  `Deployment/agent-canvas` both before and after the post-renderer runs
  (57 total documents, unchanged count across the post-render step).
- Zero `Job` resources anywhere in the stream - confirming no second
  migration runner is introduced; the chart's own `migrate-db` init
  container on `Deployment/openhands` runs with `image:
  registry.example.com/mars-enterprise:1.97.1-mars-doks.1` (the test
  fill-in's placeholder MARS-patched image), i.e. the *same* image this
  overlay sets for the app container.
- After the post-renderer: `mars-secrets` appears in `Deployment/openhands`'s
  `volumes` and in its `openhands` container's `volumeMounts`; `automation`
  and `agent-canvas` are unaffected (`automation` keeps only its own
  `workspace` volume, `agent-canvas` keeps none).
- All six `env.DO_MARS_*` values set in `openhands-mars.values.yaml` land
  on the `openhands` container exactly as supplied, confirming the chart's
  user-override-wins merge in `templates/_env.yaml` behaves as documented.
- `helm template` of `helm/charts/mars-doks` with the exact copy-ready
  values above (gatewayUrl/database/doMars/secrets/ingress all filled in)
  succeeds with no `required` failures, and both `mars-gateway`'s and
  `mars-pool-reconciler`'s rendered pod `securityContext` show
  `runAsUser: 42420, runAsGroup: 42420, runAsNonRoot: true, fsGroup: 42420`
  - the fsGroup fix needed so kubelet-projected, mode-0440 `mars-secrets`
    files are group-readable by the pod's own non-root user, rather than
    landing root:root and unreadable.

### What this is not evidence of

This is a template-rendering proof against representative placeholder
values, run with no live DOKS cluster, no real managed PostgreSQL/object
storage, and no real DigitalOcean API access. It does not verify a
Kubernetes API server accepts these manifests, that the patched image
actually boots against a real database, or that the gateway/preview flows
work end-to-end against real infrastructure. See [versions and validation](VERSIONS.md) and the [callback preflight](CALLBACKS.md).

## `render-fixture.values.yaml`

Exists only to make the render-proof above tractable in an evaluation
environment where none of this install's actual bundled/external infra is
reachable. It is not an install overlay; never layer it onto a real
release (see the comment header in that file for the full rationale).
