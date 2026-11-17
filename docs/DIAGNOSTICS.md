# MARS diagnostic logging

All provider, tunnel and gateway diagnostics go through `do_mars_diagnostics.log_diagnostic`, which writes to the logger `openhands.app_server.sandbox.do_mars_diagnostics`. Every field is checked against a closed vocabulary or a strict shape, and anything that fails the check is dropped. As a result, a record can contain:

- an event name and an operation label;
- the HTTP method;
- an HTTP status, WebSocket handshake status or WebSocket close code;
- the exception **class name**;
- a DigitalOcean `SESSION_STATUS_*` value. Any other provider status is logged as `unrecognized`.

A record **never** contains exception text, a URL, a path or query, headers, cookies, a token or key, a request or response body, a provider text frame, or a sandbox, session or config identifier. No traceback is attached (`exc_info` is not used). This is enforced by `tests/unit/app_server/test_do_mars_diagnostics.py`. Those tests place marker strings in the URLs, exception messages, provider bodies, request paths, cookies, request bodies and identifiers. They then assert that the expected fields are logged and that no marker, and no `127.0.0.1` or `api.digitalocean` host, appears in any OpenHands log record.

Because these records carry no identifiers, correlate them with Enterprise state by time and process, not by ID.

| Event | Emitted by | Fields |
|---|---|---|
| `do_mars.control_response` | DO control API reply with status ≥ 300 (a 404 is logged at INFO, because it is the normal end of delete polling) | `operation` (`session.create\|get\|list\|pause\|resume\|delete`), `method`, `status_code` |
| `do_mars.control_transport_error` | DO control API request that got no response | `operation`, `method`, `error_class` |
| `do_mars.delete_unconfirmed` | Enterprise delete that returns 503 (`SandboxDeleteRetryError`) | `reason` = `control_error` (+`error_class`), `confirmation_timeout` (+ last `provider_status`) or `unresolved_intent` |
| `do_mars.cleanup_after_init_failure_failed` | Best-effort delete after a failed create or init | `operation`, `error_class` |
| `do_mars.init_failed` | Deferred `/api/init` probe or post | `operation` (`init.probe\|init.post`), `reason` (`bad_status\|not_dormant`), `status_code` |
| `do_mars.idle_probe_failed` | Lifecycle idle probe | `reason` (`bad_status`+`status_code`, or `transport_error`+`error_class`) |
| `do_mars.tunnel_ws_failed` | `tcp_forward` provider WebSocket | `phase` (`handshake\|stream`), `handshake_status`, `error_class` |
| `do_mars.tunnel_ws_closed` | Provider WebSocket closed abnormally, or sent a TEXT/ERROR frame | `phase`, `close_code` or `frame_type` (only the type; the frame content is never logged) |
| `do_mars.tunnel_request_failed` | Control-side guest request over the tunnel (init) | `method`, `reason`, `error_class`, `attempts` |
| `do_mars.gateway_guest_response` | Gateway HTTP: the guest answered ≥ 400 | `operation=gateway.http`, `method`, `status_code` |
| `do_mars.gateway_guest_transport_error` | Gateway HTTP: no guest response (502) | `operation`, `method`, `error_class` |
| `do_mars.gateway_guest_ws_failed` | Gateway WebSocket to the guest | `operation=gateway.ws`, `phase`, `handshake_status`, `error_class` |

## Reading a failed automatic delete (Enterprise 503)

An Enterprise `DELETE` that returns **503** for a MARS sandbox is `SandboxDeleteRetryError('DigitalOcean MARS deletion is unconfirmed')`. In this code it has exactly four sources, and the new records tell them apart:

1. DO answered the `DELETE` with a status other than 200, 202, 204 or 404, or answered a confirmation `GET` with a status of 400 or higher other than 404. You will see `control_response operation=session.delete|session.get status_code=…`, then `delete_unconfirmed reason=control_error`.
2. The `DELETE` or a confirmation `GET` got no response. You will see `control_transport_error`, then `delete_unconfirmed reason=control_error`.
3. DO accepted the delete, but within 60 s the session never returned 404, `SESSION_STATUS_DESTROYED` or `SESSION_STATUS_DELETED`. You will see `delete_unconfirmed reason=confirmation_timeout provider_status=…`. If that status is a terminal value this code does not treat as gone, that points to a status-mapping gap. It would have to be confirmed against the DO API before any change.
4. The row has no provider id and its create is unresolved: `delete_unconfirmed reason=unresolved_intent`.

The **root cause of the ten 503s in the live ten-run attempt is not established**. This package only adds the records that will identify it on the next run, with the evidence already preserved by the coordinator. It does not change the delete semantics.

The MARS native-start probe emits `do_mars.health_probe_failed` with operation `health.probe`. Transport exceptions carry only a bounded error class at DEBUG; refused authentication (401/403) is WARNING, other unsuccessful statuses are DEBUG. Keys, URLs and response bodies are excluded. A healthy sandbox does not establish model inference: inspect conversation errors separately for egress failures, authentication and exhausted provider budgets.
