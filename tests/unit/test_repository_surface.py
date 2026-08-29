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

    def test_makefile_exposes_only_declared_operation_families(self) -> None:
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        targets = set(re.findall(r"(?m)^([a-zA-Z0-9_-]+):", makefile))
        operational = targets - {"help", "check", "check-static", "lint", "syntax"}
        self.assertTrue(operational)
        # Семейств два, и они разделены намеренно. `fleet-` трогает серверы.
        # `operator-` не трогает их вовсе: выдача и отзыв доступа правят только
        # объявления в Git и оставляют диффф под ревью, а применяет его отдельная
        # защищённая операция. Общий префикс скрыл бы эту разницу, а список без
        # префиксов перестал бы быть ограничителем.
        families = ("fleet-", "operator-")
        unexpected = sorted(
            target for target in operational if not target.startswith(families)
        )
        self.assertEqual(unexpected, [], "цель вне объявленных семейств операций")
        self.assertNotIn("ALLOW_LEGACY", makefile)

if __name__ == "__main__":
    unittest.main()
