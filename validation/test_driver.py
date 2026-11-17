"""Boundary tests for the operator driver; no network or credential values."""

import contextlib
import importlib.util
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "mars_driver", Path(__file__).with_name("run_native_validation.py")
)
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


class DriverTest(unittest.TestCase):
    def test_wrong_org_stops_before_any_write(self):
        user = {
            "email": "operator@example.invalid",
            "org_id": "evaluation",
            "id": "operator",
        }
        key = {"bound_org_id": "other", "user_id": "operator"}
        env = {
            "OPENHANDS_HOST": "https://app.example.invalid",
            "OPENHANDS_ORG_ID": "evaluation",
            "OPENHANDS_EXPECTED_EMAIL": user["email"],
            "OPENHANDS_API_KEY": "synthetic-fixture",
        }
        with (
            patch.dict(driver.os.environ, env),
            patch.object(
                driver.OPENER,
                "open",
                side_effect=[
                    io.BytesIO(json.dumps(user).encode()),
                    io.BytesIO(json.dumps(key).encode()),
                ],
            ),
            patch.object(driver, "api") as write,
            patch("sys.argv", ["driver"]),
        ):
            with self.assertRaises(ValueError):
                driver.main()
            write.assert_not_called()

    def test_ten_standard_dispatches_keep_inactive_history(self):
        dispatches = []
        calls = []

        def api(method, path, body=None, *args):
            calls.append((method, path))
            if path.startswith("/uploads"):
                return {"tarball_path": "oh-internal://uploads/synthetic"}
            if method == "POST" and path == "":
                self.assertFalse(body["enabled"])
                self.assertEqual(body["state"], "INACTIVE")
                self.assertFalse(body["keep_alive"])
                return {"id": "fixture", "state": "INACTIVE", "enabled": False}
            if path.endswith("/dispatch"):
                run = {
                    "id": str(len(dispatches)),
                    "sandbox_id": "vm-" + str(len(dispatches)),
                    "status": "COMPLETED",
                    "run_metadata": {"task_outcome": {"passed": True}},
                }
                dispatches.append(run)
                return run
            if method == "GET":
                return {"runs": dispatches}
            self.fail("Unexpected API operation")

        with (
            patch.object(driver, "qualify"),
            patch.object(driver, "tarball", return_value=b"synthetic"),
            patch.object(driver, "api", side_effect=api),
            patch("sys.argv", ["driver", "--runs", "10"]),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(driver.main(), 0)
        self.assertEqual(len(dispatches), 10)
        self.assertTrue(json.loads(output.getvalue())["passed"])
        self.assertNotIn("DELETE", [method for method, _ in calls])

    def test_ambiguous_dispatch_is_not_replayed(self):
        dispatched = 0

        def api(method, path, body=None, *args):
            nonlocal dispatched
            if path.startswith("/uploads"):
                return {"tarball_path": "oh-internal://uploads/synthetic"}
            if path == "":
                return {"id": "fixture", "state": "INACTIVE", "enabled": False}
            dispatched += 1
            raise TimeoutError("synthetic ambiguous response")

        with (
            patch.object(driver, "qualify"),
            patch.object(driver, "tarball", return_value=b"synthetic"),
            patch.object(driver, "api", side_effect=api),
            patch("sys.argv", ["driver", "--runs", "10"]),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(TimeoutError),
        ):
            driver.main()
        self.assertEqual(dispatched, 1)


if __name__ == "__main__":
    unittest.main()
