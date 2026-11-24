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
from the repository root with Helm 3. The post-renderer needs Python 3 and
PyYAML. Keep it in every Helm upgrade while MARS is enabled. Helm 4
requires a [post-renderer plugin](https://helm.sh/docs/plugins/migrate/)
for this helper. The commands below use Helm 3's executable interface.

Build and push the [Enterprise image](../docker/enterprise.Dockerfile)
to a registry your cluster can pull from:

```sh
docker build -f doks/docker/enterprise.Dockerfile -t "$ENTERPRISE_IMAGE" .
docker push "$ENTERPRISE_IMAGE"
```

Set `ENTERPRISE_IMAGE` to a fully qualified registry name and tag before
running those commands. For the optional Pause and preview UI, also
[build and push the Canvas image](../../docs/CANVAS.md#packaged-as-a-docker-image).

## How the post-renderer configures MARS

The official chart renders its general environment values as strings.
The post-renderer sets the MARS sandbox and Sandbox Spec injectors and
mounts a Kubernetes Secret on the Enterprise containers that use them.
This includes the app and its migration init container. The mount uses
`/run/mars-secrets` so it can coexist with the Kubernetes service-account
mount. Canvas, Automation and Helm upgrade hooks use their own
configuration. The Secret files are group-readable under the official
chart's Enterprise pod security context.

The post-renderer verifies the intended Enterprise Deployment and
container name before emitting any manifests. For an installation that
overrides those names, configure `MARS_POSTRENDER_DEPLOYMENT` and
`MARS_POSTRENDER_CONTAINER` explicitly.

## Scope rule for `openhands-mars.values.yaml`

`helm/values/openhands-mars.values.yaml` adds only what MARS itself
requires: `image.repository`/`tag` (the MARS-patched Enterprise image),
`env.DO_MARS_*` (provider config), and `agent-canvas.image.*` +
`agent-canvas.staticServer.lockToCloud` (the Canvas pilot build). The post-renderer sets the MARS injector selectors. The values file
leaves `postgresql`/`keycloak`/`redis`/`valkey`/`minio`/
`litellm-helm`, `ingress.*`, `externalDatabase.*`, and `automation.*`
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

## Live EKS smoke test

One test on November 24, 2026 used the official OpenHands chart 0.119.0,
patched Enterprise 1.97.1 and patched Canvas 1.45.0 in a disposable EKS
namespace. It used a fresh PostgreSQL database, one ready MARS VM, a
synthetic chat message, and a small live preview process. The browser
launch called Enterprise's native conversation start endpoint.

| Observation | Time |
| --- | ---: |
| Native warm launch to Ready | 20.84 s |
| Reopen click to chat history visible | 0.13 s |
| Reopen click to successful resume response | 5.01 s |
| Reopen click to a successful interaction in the embedded preview | 10.78 s |

The provider ID matched the VM in the warm pool before launch. Clicking
Pause in Canvas paused that VM. A preview request during the pause was
rejected and left the VM paused. Reopening the original conversation
restored the same VM, preview process and in-memory counter. An
anonymous request to the live preview returned HTTP 401.

This run used public HTTPS with existing DNS and an exact-host
certificate for the test preview. Validate private-only callback DNS,
routing and TLS separately on DOKS. The test used no paid model inference.

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
- The post-renderer adds the MARS injector selectors and Secret mount to
  patched Enterprise containers that declare the MARS token file.
  Canvas and Automation keep their own environment and volumes.
- `helm template` of `helm/charts/mars-doks` with the exact copy-ready
  values above (gatewayUrl/database/doMars/secrets/ingress all filled in)
  succeeds with no `required` failures, and both `mars-gateway`'s and
  `mars-pool-reconciler`'s rendered pod `securityContext` show
  `runAsUser: 42420, runAsGroup: 42420, runAsNonRoot: true, fsGroup: 42420`
  - the fsGroup fix needed so kubelet-projected, mode-0440 `mars-secrets`
    files are group-readable by the pod's own non-root user, rather than
    landing root:root and unreadable.

### What this is not evidence of

The render checks use representative placeholder values. For the live
EKS result, see the smoke test above. Use the
[callback preflight](CALLBACKS.md) on an actual DOKS target.

## `render-fixture.values.yaml`

Exists only to make the render-proof above tractable in an evaluation
environment where none of this install's actual bundled/external infra is
reachable. It is not an install overlay; never layer it onto a real
release (see the comment header in that file for the full rationale).
