# MARS networking on DOKS

The MARS gateway and optional warm-pool worker run inside the OpenHands namespace. The MARS VM runs the workspace and Agent Server. Neither control-plane workload requires a separate Droplet.

| Connection | Address |
| --- | --- |
| Enterprise to the runtime gateway | `DO_MARS_INTERNAL_URL`, the gateway's Kubernetes Service on port 8100 |
| Canvas and other authorized clients to Agent Server | `DO_MARS_GATEWAY_URL`, a dedicated HTTPS gateway hostname. The gateway authenticates the session and forwards to the VM through the provider tunnel. |
| MARS Agent Server to Enterprise | `DO_MARS_CALLBACK_URL`, an HTTPS Enterprise origin reachable from the VM. The adapter appends `/api/v1/webhooks`. |
| Browser to the live preview | `https://<sandbox-id>.preview.<app-hostname>`, routed to the gateway with a short-lived, sandbox-scoped preview cookie |

## DOKS with isolated worker nodes

The app and gateway can use public HTTPS Ingress backed by a DOKS HTTP load balancer while worker nodes have private addresses. Route the app hostname to the official OpenHands Ingress and the gateway hostname to the companion chart's Service. Preserve the original Host header and WebSocket upgrades on the gateway and preview routes. Allow the Enterprise callback hostname in the MARS environment's outbound policy if you restrict egress. [DigitalOcean isolated worker node documentation](https://docs.digitalocean.com/products/kubernetes/how-to/create-clusters-with-isolated-worker-nodes/)

## Private-only Enterprise callback

Use a VPC-reachable internal HTTPS frontend for the callback. Confirm with DigitalOcean that the selected MARS environment can reach the VPC and region that host the DOKS internal load balancer. From a disposable VM using that same environment, resolve the callback hostname and run `curl --fail --silent --show-error --max-time 10 "$DO_MARS_CALLBACK_URL/health"`. Check that DNS returns the intended private address and that TLS validates without disabling certificate checks. Then start one real Canvas conversation and confirm an authenticated Agent Server callback arrives in Enterprise. A successful health request checks network connectivity. The conversation checks routing and authentication. [DigitalOcean MicroVM networking](https://docs.digitalocean.com/products/microvms/concepts/networking-access/), [DOKS internal load balancers](https://docs.digitalocean.com/products/kubernetes/how-to/configure-load-balancers/)

If the VM lacks VPC connectivity, use the authenticated public HTTPS callback path where your network policy permits it. Test private reachability from the VM itself, since a successful probe from a DOKS pod only checks the cluster side.

## Browser preview

Set `ingress.preview.enabled: true`, set `ingress.preview.host` to `*.preview.<app-hostname>`, and provide the wildcard certificate Secret through `ingress.preview.tlsSecretName`. Point wildcard DNS to the ingress controller. The gateway uses the hostname to select a sandbox and validates its preview cookie. The app and gateway must agree on the public app origin. Use the opt-in conversation tag described in [Canvas](../../docs/CANVAS.md#operator-opt-in-per-conversation). Verify an authorized owner can Pause, reopen the same conversation, and see its existing preview. Also check that a private browser session and a user from another conversation receive no preview content.

## Gateway database role

Use `database.gatewayExistingSecret` to give the gateway the [restricted read-only role](../../docs/INSTALL_MANUAL.md#4-gateway-database-role-recommended). Create that role after the Enterprise migration adds its required columns. The warm-pool worker separately uses `database.existingSecret` and requires a session-preserving connection to the same database and schema as Enterprise.
