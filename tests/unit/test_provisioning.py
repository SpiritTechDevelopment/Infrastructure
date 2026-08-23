from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

from fleetctl.provisioning import ManualProvisioningAdapter
from fleetctl.validation import validate_environment


REPO_ROOT = Path(__file__).resolve().parents[2]
VALID_DESIRED = REPO_ROOT / "tests" / "fixtures" / "valid" / "desired"


class ManualProvisioningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        state = validate_environment(REPO_ROOT, "develop", desired_root=VALID_DESIRED)
        cls.instance = state.instances[0]
        cls.adapter = ManualProvisioningAdapter()

    def test_describe_is_an_operator_declaration_not_a_fake_provider_lookup(self) -> None:
        description = self.adapter.describe(self.instance)
        self.assertEqual(description.instance_id, self.instance.object_id)
        self.assertEqual(description.evidence, "desired_state_operator_declaration")

    def test_documentation_address_fails_real_server_preflight(self) -> None:
        report = self.adapter.preflight(self.instance)
        self.assertFalse(report.passed)
        failed = {check.name for check in report.checks if not check.passed}
        self.assertIn("public_address", failed)

    def test_real_manual_declaration_passes_preflight(self) -> None:
        instance = replace(
            self.instance,
            public_address="1.1.1.1",
            provider_resource_id="vps-240812-001",
            provider_name="manual",
        )
        report = self.adapter.preflight(instance)
        self.assertTrue(report.passed)
        self.assertTrue(all(check.diagnostic for check in report.checks))

    def test_placeholder_resource_id_is_rejected(self) -> None:
        instance = replace(
            self.instance,
            public_address="1.1.1.1",
            provider_resource_id="REPLACE_PROVIDER_RESOURCE_ID",
        )
        report = self.adapter.preflight(instance)
        self.assertFalse(report.passed)
        resource = next(check for check in report.checks if check.name == "provider_resource_id")
        self.assertFalse(resource.passed)


if __name__ == "__main__":
    unittest.main()
