from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fleetctl.adapters import (
    CompiledArtifactsError,
    validate_ansible_artifacts,
    write_rendered_files,
)
from fleetctl.compiler import render_files
from fleetctl.validation import validate_environment


REPO_ROOT = Path(__file__).resolve().parents[2]
VALID_DESIRED = REPO_ROOT / "tests" / "fixtures" / "valid" / "desired"


class CompiledAnsibleArtifactTests(unittest.TestCase):
    def render(self, parent: Path) -> Path:
        state = validate_environment(REPO_ROOT, "develop", desired_root=VALID_DESIRED)
        output = parent / "develop"
        write_rendered_files(output, render_files(state))
        return output

    def test_generated_inventory_and_node_plans_form_one_valid_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = self.render(Path(temporary))
            self.assertEqual(validate_ansible_artifacts(output, "develop"), 2)

    def test_unsupported_node_plan_version_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = self.render(Path(temporary))
            path = output / "node-plans" / "develop-entry-nl-01.json"
            plan = json.loads(path.read_text(encoding="utf-8"))
            plan["schema_version"] = 2
            path.write_text(json.dumps(plan), encoding="utf-8")
            with self.assertRaises(CompiledArtifactsError):
                validate_ansible_artifacts(output, "develop")

    def test_inventory_cannot_smuggle_domain_variables_around_node_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = self.render(Path(temporary))
            path = output / "ansible-inventory.json"
            inventory = json.loads(path.read_text(encoding="utf-8"))
            hosts = inventory["all"]["children"]["spiritvpn_fleet"]["children"]["entry"]["hosts"]
            hosts["develop-entry-nl-01"]["node_limits_egress_limit_mbps"] = 1
            path.write_text(json.dumps(inventory), encoding="utf-8")
            with self.assertRaises(CompiledArtifactsError):
                validate_ansible_artifacts(output, "develop")

    def test_stale_node_plan_not_referenced_by_inventory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = self.render(Path(temporary))
            stale = output / "node-plans" / "stale.json"
            stale.write_text("{}\n", encoding="utf-8")
            with self.assertRaises(CompiledArtifactsError):
                validate_ansible_artifacts(output, "develop")

    def test_new_configure_contour_loads_compiled_plan_and_does_not_read_desired(self) -> None:
        playbook = (REPO_ROOT / "playbooks" / "deploy" / "configure.yml").read_text(encoding="utf-8")
        loader = (REPO_ROOT / "roles" / "compiled_node_plan" / "tasks" / "main.yml").read_text(
            encoding="utf-8"
        )
        combined = playbook + loader
        self.assertIn("compiled_node_plan", playbook)
        self.assertIn("spiritvpn_node_plan_file", loader)
        self.assertNotIn("desired/", combined)
        self.assertNotIn("fleet-entries.yml", playbook)
        self.assertNotIn("fleet-exits.yml", playbook)
        self.assertIn("common_restricted_tcp_rules", loader)
        self.assertIn("infrastructure.networking.agent.port", loader)


if __name__ == "__main__":
    unittest.main()
