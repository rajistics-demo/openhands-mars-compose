"""Read-only release Compose contract check; no Docker daemon or credentials.

ENTERPRISE_COMPOSE=/path/to/1.88.0/docker-compose.yml python3 scripts/test_compose_contract.py
"""

import json
import os
import subprocess
import unittest
from pathlib import Path


class ComposeContractTest(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("ENTERPRISE_COMPOSE"),
        "set ENTERPRISE_COMPOSE to the stock 1.88.0 file",
    )
    def test_public_callback_and_private_control_paths(self):
        package = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                os.environ["ENTERPRISE_COMPOSE"],
                "-f",
                str(package / "compose/docker-compose.mars.yml"),
                "--env-file",
                str(package / "compose/mars.env.example"),
                "config",
                "--format",
                "json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode,
            0,
            "Compose merge failed; inspect config locally without publishing its values",
        )
        services = json.loads(result.stdout)["services"]
        automation = services["automation-server"]["environment"]
        self.assertEqual(
            automation["AUTOMATION_BASE_URL"], "https://app.example.invalid"
        )
        self.assertEqual(
            automation["AUTOMATION_SANDBOX_OPENHANDS_API_URL"],
            "https://app.example.invalid",
        )
        self.assertEqual(
            automation["AUTOMATION_OPENHANDS_API_BASE_URL"], "http://app:3000"
        )
        self.assertEqual(automation["AUTOMATION_SANDBOX_INTERNAL_URL"], "")
        for name in ["app", "worker"]:
            self.assertEqual(
                str(services[name]["environment"]["OH_SANDBOX_MAX_NUM_SANDBOXES"]), "10"
            )
            self.assertFalse(
                any("docker.sock" in str(v) for v in services[name].get("volumes", []))
            )


if __name__ == "__main__":
    unittest.main()
