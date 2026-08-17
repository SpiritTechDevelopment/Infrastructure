from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


class RepositorySurfaceTests(unittest.TestCase):
    def test_only_v1_deployment_surfaces_are_tracked(self) -> None:
        tracked = {
            path
            for path in subprocess.check_output(
                ["git", "ls-files"], cwd=REPO_ROOT, text=True
            ).splitlines()
            if (REPO_ROOT / path).exists()
        }
        forbidden_roots = (
            "inventories/prod/",
            "inventories/dev/",
            "roles/acme/",
            "roles/alloy/",
            "roles/backend/",
            "roles/cloudflare_dns/",
            "roles/management_wireguard/",
            "roles/observability/",
            "roles/vault/",
            "roles/vpn_stack/",
        )
        unexpected = sorted(
            path for path in tracked if any(path.startswith(root) for root in forbidden_roots)
        )
        self.assertEqual(unexpected, [])

    def test_makefile_exposes_only_fleet_operations(self) -> None:
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        targets = set(re.findall(r"(?m)^([a-zA-Z0-9_-]+):", makefile))
        operational = targets - {"help", "check", "lint", "syntax"}
        self.assertTrue(operational)
        self.assertTrue(all(target.startswith("fleet-") for target in operational))
        self.assertNotIn("ALLOW_LEGACY", makefile)

    def test_scripts_are_bounded_to_the_v1_control_plane(self) -> None:
        scripts = {
            path.name
            for path in (REPO_ROOT / "scripts").iterdir()
            if path.is_file() and path.name != "__pycache__"
        }
        self.assertEqual(
            scripts,
            {
                "platform-bootstrap-check.py",
                "platform-bootstrap.sh",
                "bootstrap-platform.py",
                "platform-sops.py",
                "platform-remote.sh",
                "bootstrap-self-hosted-runner.sh",
                # Joins that runner to the management overlay; the hub is only
                # reachable there, so registering the runner is part of the
                # same bootstrap as installing it.
                "enroll-runner-overlay.sh",
                "vault-secret-resolver.py",
                "vendor-backend-contract.sh",
            },
        )


if __name__ == "__main__":
    unittest.main()
