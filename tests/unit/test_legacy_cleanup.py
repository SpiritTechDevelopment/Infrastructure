from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


class LegacyCleanupTests(unittest.TestCase):
    def test_legacy_production_workflows_are_not_executable(self) -> None:
        self.assertFalse((REPO_ROOT / ".github" / "workflows" / "deploy.yml").exists())
        gitlab = (REPO_ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")
        self.assertNotIn("stage: deploy", gitlab)
        self.assertNotIn("make deploy", gitlab)
        self.assertNotIn("inventories/prod/inventory.yml", gitlab)

    def test_only_fleet_deploy_keeps_the_unqualified_deploy_name(self) -> None:
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        retired = ("deploy", "platform", "wire", "dns", "apply-node", "check-node", "reconcile")
        for target in retired:
            self.assertIsNone(re.search(rf"(?m)^{re.escape(target)}\s*:", makefile))
            self.assertRegex(makefile, rf"(?m)^legacy-{re.escape(target)}\s*:")

    def test_legacy_operations_require_explicit_break_glass_override(self) -> None:
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        ansible_config = (REPO_ROOT / "ansible.cfg").read_text(encoding="utf-8")
        self.assertIn("legacy-guard:", makefile)
        self.assertIn('test "$(ALLOW_LEGACY)" = 1', makefile)
        self.assertIn("legacy-deploy: legacy-decrypt", makefile)
        self.assertIn("legacy-decrypt: legacy-guard", makefile)
        self.assertIn("LEGACY_INVENTORY ?= inventories/prod/inventory.yml", makefile)
        self.assertNotIn("inventory = inventories/prod/inventory.yml", ansible_config)


if __name__ == "__main__":
    unittest.main()
