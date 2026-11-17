"""One normal native automation run executes one job in its assigned VM.

No model inference, child sandboxes, or self-directed terminal RPCs. Optional
transport checks exercise read-only Enterprise/gateway calls separately.
"""

import argparse
import json
import os
import pathlib
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

EXPECTED_SUM = 333283335000  # Synthetic arithmetic, not an infrastructure identifier.


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def request_json(url, key, *, session=False):
    header = "X-Session-API-Key" if session else "Authorization"
    value = key if session else "Bearer " + key
    req = urllib.request.Request(
        url, headers={header: value, "Accept": "application/json"}
    )
    with OPENER.open(req, timeout=30) as response:
        return json.load(response)


def failure(exc, phase):
    result = {"phase": phase, "error_class": type(exc).__name__}
    if isinstance(exc, urllib.error.HTTPError):
        result["http_status"] = exc.code
    elif isinstance(exc, urllib.error.URLError):
        result["reason_class"] = type(exc.reason).__name__
        result["errno"] = getattr(exc.reason, "errno", None)
    return result


def fire_callback(result):
    body = {
        "status": "COMPLETED" if result["passed"] else "FAILED",
        "run_id": os.environ["AUTOMATION_RUN_ID"],
        "task_outcome": result,
    }
    if not result["passed"]:
        body["error"] = json.dumps(result["failure"], sort_keys=True)
    key = (
        os.environ.get("AUTOMATION_CALLBACK_API_KEY") or os.environ["OPENHANDS_API_KEY"]
    )
    req = urllib.request.Request(
        os.environ["AUTOMATION_CALLBACK_URL"],
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    with OPENER.open(req, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError("completion callback did not return 200")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hold", type=int, default=120)
    parser.add_argument("--check-transport", action="store_true")
    args = parser.parse_args()
    if not 0 <= args.hold <= 1200:
        raise ValueError("hold must be between zero and 1200 seconds")
    result = {
        "run_id": os.environ["AUTOMATION_RUN_ID"],
        "sandbox_id": os.environ["SANDBOX_ID"],
        "passed": False,
        "workload": "local arithmetic, 64 MiB and bounded hold",
        "results": [],
    }
    phase = "local_job"
    try:
        path = pathlib.Path("/workspace/mars-native-result.json")
        if path.exists():
            raise RuntimeError("workspace was reused")
        marker = secrets.token_hex(12)
        stat = pathlib.Path("/proc/self/stat").read_text().split()
        proof = {
            "marker": marker,
            "sum_squares": sum(x * x for x in range(10000)),
            "job_pid": os.getpid(),
            "process_start_ticks": int(stat[21]),
            "started_at": time.time(),
            "hold_seconds": args.hold,
        }
        if proof["sum_squares"] != EXPECTED_SUM:
            raise RuntimeError("arithmetic mismatch")
        memory = bytearray(64 * 1024 * 1024)
        for i in range(0, len(memory), 4096):
            memory[i] = 17
        path.write_text(json.dumps(proof))
        print("MARS_NATIVE_JOB_STARTED", flush=True)
        if args.check_transport:
            phase = "enterprise_lookup"
            origin = os.environ["OPENHANDS_CLOUD_API_URL"].rstrip("/")
            rows = request_json(
                origin
                + "/api/v1/sandboxes?"
                + urllib.parse.urlencode({"id": result["sandbox_id"]}),
                os.environ["OPENHANDS_API_KEY"],
            )
            own = rows[0]
            phase = "runtime_read"
            runtime = next(
                x["url"] for x in own["exposed_urls"] if x["name"] == "AGENT_SERVER"
            )
            info = request_json(
                runtime.rstrip("/") + "/server_info",
                own["session_api_key"],
                session=True,
            )
            result["transport_check"] = {
                "passed": True,
                "agent_server_version": info.get("version"),
            }
        phase = "local_hold"
        deadline = time.monotonic() + args.hold
        while time.monotonic() < deadline:
            time.sleep(min(5, max(0, deadline - time.monotonic())))
            print("MARS_NATIVE_JOB_HEARTBEAT", flush=True)
        if json.loads(path.read_text()) != proof or memory[0] != 17:
            raise RuntimeError("workspace or memory verification failed")
        result["results"] = [
            {
                "marker": marker,
                "sum_squares": EXPECTED_SUM,
                "verified": True,
                "hold_seconds": args.hold,
                "started_at": proof["started_at"],
                "finished_at": time.time(),
            }
        ]
        result["passed"] = True
    except Exception as exc:  # noqa: BLE001 - persist only bounded failure fields
        result["failure"] = failure(exc, phase)
    pathlib.Path("/workspace/mars-native-validation.json").write_text(
        json.dumps(result)
    )
    print("MARS_NATIVE_VALIDATION " + json.dumps(result), flush=True)
    try:
        fire_callback(result)
    except Exception as exc:  # noqa: BLE001 - persist only bounded failure fields
        status = failure(exc, "completion_callback")
        pathlib.Path("/workspace/mars-native-callback-status.json").write_text(
            json.dumps(status)
        )
        print("MARS_NATIVE_CALLBACK_FAILED " + json.dumps(status), flush=True)
        raise SystemExit(1) from None
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
