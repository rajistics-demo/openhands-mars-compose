#!/usr/bin/env python3
"""Operator driver: one native Automation run -> one MARS VM, no LLM.

Uploads validation/native_job, creates one inactive custom-script automation,
dispatches 1, 5 or 10 independent native runs, and verifies persisted outcomes.
Retains the inactive definition and its history. Exit 0 only when the run is COMPLETED and its stored
``run_metadata.task_outcome.passed`` is true.

Environment (placeholders; never commit real values):
  OPENHANDS_HOST     https://app.example.invalid   (Enterprise public origin)
  OPENHANDS_API_KEY  an org-bound evaluation-user key, short-lived preferred
  OPENHANDS_ORG_ID   intended evaluation organization ID
  OPENHANDS_EXPECTED_EMAIL  intended evaluation principal

This exercises the standard native path only. It does not create child runs or qualify maximum capacity. --runs dispatches
ordinary native runs; it adds no product orchestration behavior.
"""

import argparse
import gzip
import io
import json
import os
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

JOB_DIR = Path(__file__).resolve().parent / "native_job"
RUN_TIMEOUT_SECONDS = int(os.environ.get("MARS_VALIDATION_RUN_TIMEOUT", "1200"))
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "SKIPPED"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def api(method, path, body=None, content_type="application/json"):
    host = os.environ["OPENHANDS_HOST"].rstrip("/")
    data = (
        body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
    )
    request = urllib.request.Request(
        f"{host}/api/automation/v1{path}",
        method=method,
        data=data,
        headers={
            "Authorization": f"Bearer {os.environ['OPENHANDS_API_KEY']}",
            "Content-Type": content_type,
            "X-Org-Id": os.environ["OPENHANDS_ORG_ID"],
        },
    )
    with OPENER.open(request, timeout=60) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def tarball() -> bytes:
    """Deterministic archive: fixed order, mtime and ownership."""
    buffer = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        for path in sorted(JOB_DIR.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            info = tar.gettarinfo(str(path), arcname=str(path.relative_to(JOB_DIR)))
            info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, "", ""
            with path.open("rb") as handle:
                tar.addfile(info, handle)
    return buffer.getvalue()


def qualify():
    """Verify the intended principal and the credential's actual org binding."""
    host = os.environ["OPENHANDS_HOST"].rstrip("/")
    parsed = urllib.parse.urlsplit(host)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.query
        or parsed.fragment
        or parsed.path
    ):
        raise ValueError("OPENHANDS_HOST must be a plain HTTPS origin")
    headers = {
        "Authorization": "Bearer " + os.environ["OPENHANDS_API_KEY"],
        "X-Org-Id": os.environ["OPENHANDS_ORG_ID"],
    }
    values = []
    for path in ["/api/v1/users/me", "/api/keys/current"]:
        with OPENER.open(
            urllib.request.Request(host + path, headers=headers), timeout=30
        ) as response:
            values.append(json.load(response))
    user, key = values
    org = os.environ["OPENHANDS_ORG_ID"]
    if (
        user.get("email") != os.environ["OPENHANDS_EXPECTED_EMAIL"]
        or user.get("org_id") != org
        or key.get("bound_org_id") != org
        or key.get("user_id") != user.get("id")
    ):
        raise ValueError(
            "Principal or org-bound credential does not match the intended target"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runs",
        type=int,
        choices=[1, 5, 10],
        default=1,
        help="independent native dispatches; one VM each",
    )
    parser.add_argument("--hold", type=int, default=120)
    parser.add_argument(
        "--check-transport",
        action="store_true",
        help="also exercise the public runtime; allow its exact hostname in MARS egress",
    )
    args = parser.parse_args()
    if not 0 <= args.hold <= 1200 or RUN_TIMEOUT_SECONDS < args.hold + 300:
        raise ValueError(
            "hold must be 0..1200 and run timeout must allow hold plus 300 seconds"
        )
    qualify()  # No upload/create/dispatch before identity and key binding match.
    upload = api(
        "POST", "/uploads?name=mars-native-validation", tarball(), "application/gzip"
    )
    automation = api(
        "POST",
        "",
        {
            "name": "MARS native validation (deterministic, no LLM)",
            "trigger": {"type": "cron", "schedule": "0 0 1 1 *", "timezone": "UTC"},
            "tarball_path": upload["tarball_path"],
            "entrypoint": "python3 main.py --hold "
            + str(args.hold)
            + (" --check-transport" if args.check_transport else ""),
            "timeout": RUN_TIMEOUT_SECONDS,
            "enabled": False,
            "state": "INACTIVE",
            "keep_alive": False,
        },
    )
    automation_id = automation["id"]
    if automation.get("enabled") or automation.get("state") != "INACTIVE":
        raise RuntimeError(
            "Server did not preserve the inactive definition; no run dispatched"
        )
    report = {
        "automation_id": automation_id,
        "requested_runs": args.runs,
        "automation_retained_inactive": True,
        "run_ids": [],
        "passed": False,
    }
    run_ids = set()
    try:
        for _ in range(args.runs):
            # Deliberately no POST retry: reconcile an ambiguous result in the UI/API.
            dispatched = api("POST", f"/{automation_id}/dispatch")
            run_ids.add(dispatched["id"])
            report["run_ids"].append(dispatched["id"])
        if len(run_ids) != args.runs:
            raise RuntimeError("Dispatch responses were not unique")
        deadline = time.monotonic() + RUN_TIMEOUT_SECONDS + 120
        max_running = 0
        rows = []
        while time.monotonic() < deadline:
            rows = [
                r
                for r in api("GET", f"/{automation_id}/runs?limit=100")["runs"]
                if r["id"] in run_ids
            ]
            max_running = max(max_running, sum(r["status"] == "RUNNING" for r in rows))
            if len(rows) == args.runs and all(r["status"] in TERMINAL for r in rows):
                break
            time.sleep(5)
        report["max_native_running_observed"] = max_running
        report["runs"] = [
            {
                "id": r["id"],
                "sandbox_id": r.get("sandbox_id"),
                "status": r["status"],
                "task_outcome": (r.get("run_metadata") or {}).get("task_outcome"),
            }
            for r in rows
        ]
        report["passed"] = (
            len(rows) == args.runs
            and len({r.get("sandbox_id") for r in rows}) == args.runs
            and all(
                r["status"] == "COMPLETED"
                and (r.get("run_metadata") or {}).get("task_outcome", {}).get("passed")
                for r in rows
            )
        )
    finally:
        # Run IDs and metadata are private evidence; save output outside git.
        # Keep the inactive definition/history. Native dispatch owns VM cleanup.
        print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
