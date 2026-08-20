from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from fleetctl.planning import PlanningError, build_impact_plan
from fleetctl.validation import validate_environment


REPO_ROOT = Path(__file__).resolve().parents[2]
VALID_DESIRED = REPO_ROOT / "tests" / "fixtures" / "valid" / "desired"


def copy_valid_desired(parent: Path, name: str) -> Path:
    target = parent / name / "desired"
    shutil.copytree(VALID_DESIRED, target)
    return target


def load_yaml(path: Path) -> dict[str, object]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def save_yaml(path: Path, document: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


class ImpactPlanningTests(unittest.TestCase):
    def test_unchanged_state_has_empty_deterministic_plan(self) -> None:
        state = validate_environment(REPO_ROOT, "develop", desired_root=VALID_DESIRED)
        first = build_impact_plan(state, state)
        second = build_impact_plan(state, state)
        self.assertEqual(first.to_json_bytes(), second.to_json_bytes())
        self.assertEqual(first.changes, ())
        self.assertFalse(first.destructive)
        self.assertEqual(first.source_digest, first.baseline_digest)

    def test_exit_replacement_expands_to_linked_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            baseline_root = copy_valid_desired(root, "baseline")
            current_root = copy_valid_desired(root, "current")
            instances = current_root / "environments" / "develop" / "instances"
            old_path = instances / "develop-exit-de-01.yml"
            replacement = load_yaml(old_path)
            old_path.unlink()
            replacement["metadata"]["id"] = "develop-exit-de-02"
            replacement["spec"]["public_address"] = "192.0.2.21"
            replacement["spec"]["provider"]["resource_id"] = "fixture-exit-02"
            save_yaml(instances / "develop-exit-de-02.yml", replacement)

            baseline = validate_environment(REPO_ROOT, "develop", desired_root=baseline_root)
            current = validate_environment(REPO_ROOT, "develop", desired_root=current_root)
            plan = build_impact_plan(current, baseline)

        change_types = {change["type"] for change in plan.changes}
        self.assertIn("INSTANCE_REPLACED", change_types)
        self.assertNotIn("INSTANCE_ADDED", change_types)
        self.assertNotIn("INSTANCE_REMOVED", change_types)
        self.assertFalse(plan.destructive)
        self.assertEqual(plan.affected["provision"], ("develop-exit-de-02",))
        self.assertEqual(plan.affected["retire"], ("develop-exit-de-01",))
        self.assertIn("develop-entry-nl-01", plan.affected["node_runtime"])
        self.assertIn("develop-entry-nl-01", plan.affected["configure"])
        self.assertIn("develop-exit-de", plan.affected["backend_nodes"])
        self.assertIn("develop-exit-de", plan.affected["dns_nodes"])

    def test_entry_replacement_affects_dns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            baseline_root = copy_valid_desired(root, "baseline")
            current_root = copy_valid_desired(root, "current")
            instances = current_root / "environments" / "develop" / "instances"
            old_path = instances / "develop-entry-nl-01.yml"
            replacement = load_yaml(old_path)
            old_path.unlink()
            replacement["metadata"]["id"] = "develop-entry-nl-02"
            replacement["spec"]["public_address"] = "192.0.2.11"
            replacement["spec"]["provider"]["resource_id"] = "fixture-entry-02"
            save_yaml(instances / "develop-entry-nl-02.yml", replacement)

            baseline = validate_environment(REPO_ROOT, "develop", desired_root=baseline_root)
            current = validate_environment(REPO_ROOT, "develop", desired_root=current_root)
            plan = build_impact_plan(current, baseline)

        self.assertIn("develop-entry-nl", plan.affected["dns_nodes"])
        self.assertIn("develop-entry-nl-02", plan.affected["monitoring"])

    def test_common_limit_change_affects_every_active_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            baseline_root = copy_valid_desired(root, "baseline")
            current_root = copy_valid_desired(root, "current")
            limits_path = current_root / "common" / "limits.yml"
            limits = load_yaml(limits_path)
            limits["bandwidth_profiles"]["vps-1g"]["port_capacity_mbps"] = 2000
            save_yaml(limits_path, limits)

            baseline = validate_environment(REPO_ROOT, "develop", desired_root=baseline_root)
            current = validate_environment(REPO_ROOT, "develop", desired_root=current_root)
            plan = build_impact_plan(current, baseline)

        common_changes = [change for change in plan.changes if change["type"] == "COMMON_CHANGED"]
        self.assertEqual(common_changes[0]["section"], "limits")
        self.assertEqual(
            plan.affected["configure"],
            ("develop-entry-nl-01", "develop-exit-de-01"),
        )
        self.assertNotEqual(plan.source_digest, plan.baseline_digest)

    def test_candidate_addition_affects_monitoring_but_not_dns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            baseline_root = copy_valid_desired(root, "baseline")
            current_root = copy_valid_desired(root, "current")
            instances = current_root / "environments" / "develop" / "instances"
            candidate = load_yaml(instances / "develop-entry-nl-01.yml")
            candidate["metadata"]["id"] = "develop-entry-nl-02"
            candidate["spec"]["target_state"] = "candidate"
            candidate["spec"]["public_address"] = "192.0.2.11"
            candidate["spec"]["provider"]["resource_id"] = "fixture-entry-02"
            save_yaml(instances / "develop-entry-nl-02.yml", candidate)

            baseline = validate_environment(REPO_ROOT, "develop", desired_root=baseline_root)
            current = validate_environment(REPO_ROOT, "develop", desired_root=current_root)
            plan = build_impact_plan(current, baseline)

        self.assertIn("develop-entry-nl-02", plan.affected["monitoring"])
        self.assertNotIn("develop-entry-nl", plan.affected["dns_nodes"])

    def test_agent_port_change_affects_monitoring_but_not_dns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            baseline_root = copy_valid_desired(root, "baseline")
            current_root = copy_valid_desired(root, "current")
            networking_path = current_root / "common" / "networking.yml"
            networking = load_yaml(networking_path)
            networking["agent"]["port"] = 9444
            save_yaml(networking_path, networking)

            baseline = validate_environment(REPO_ROOT, "develop", desired_root=baseline_root)
            current = validate_environment(REPO_ROOT, "develop", desired_root=current_root)
            plan = build_impact_plan(current, baseline)

        self.assertEqual(
            plan.affected["monitoring"],
            ("develop-entry-nl-01", "develop-exit-de-01"),
        )
        self.assertEqual(plan.affected["dns_nodes"], ())

    def test_node_override_affects_only_that_node(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            baseline_root = copy_valid_desired(root, "baseline")
            current_root = copy_valid_desired(root, "current")
            node_path = current_root / "environments" / "develop" / "nodes" / "develop-entry-nl.yml"
            node = load_yaml(node_path)
            node["spec"]["common_overrides"] = {"networking": {"agent": {"port": 9555}}}
            save_yaml(node_path, node)

            baseline = validate_environment(REPO_ROOT, "develop", desired_root=baseline_root)
            current = validate_environment(REPO_ROOT, "develop", desired_root=current_root)
            plan = build_impact_plan(current, baseline)

        changes = [change for change in plan.changes if change["type"] == "LOGICAL_NODE_CHANGED"]
        self.assertEqual(
            changes,
            [{"type": "LOGICAL_NODE_CHANGED", "node_id": "develop-entry-nl", "fields": ["common"]}],
        )
        self.assertEqual(plan.affected["configure"], ("develop-entry-nl-01",))
        self.assertEqual(plan.affected["monitoring"], ("develop-entry-nl-01",))
        self.assertEqual(plan.affected["dns_nodes"], ())

    def test_noop_override_does_not_change_digest_or_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            baseline_root = copy_valid_desired(root, "baseline")
            current_root = copy_valid_desired(root, "current")
            environment_path = current_root / "environments" / "develop" / "environment.yml"
            environment = load_yaml(environment_path)
            environment["spec"]["common_overrides"] = {"networking": {"agent": {"port": 9443}}}
            save_yaml(environment_path, environment)

            baseline = validate_environment(REPO_ROOT, "develop", desired_root=baseline_root)
            current = validate_environment(REPO_ROOT, "develop", desired_root=current_root)
            plan = build_impact_plan(current, baseline)

        self.assertEqual(plan.changes, ())
        self.assertEqual(plan.source_digest, plan.baseline_digest)

    def test_control_release_change_affects_only_local_control_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            baseline_root = copy_valid_desired(root, "baseline")
            current_root = copy_valid_desired(root, "current")
            environment_path = current_root / "environments" / "develop" / "environment.yml"
            environment = load_yaml(environment_path)
            environment["spec"]["control"]["backend_release"]["backend_image"]["digest"] = (
                "sha256:" + "f" * 64
            )
            save_yaml(environment_path, environment)

            baseline = validate_environment(REPO_ROOT, "develop", desired_root=baseline_root)
            current = validate_environment(REPO_ROOT, "develop", desired_root=current_root)
            plan = build_impact_plan(current, baseline)

        self.assertEqual(plan.affected["control"], ("develop",))
        self.assertEqual(plan.affected["configure"], ())
        self.assertEqual(plan.affected["node_runtime"], ())

    def test_bridge_removal_is_destructive_and_affects_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            baseline_root = copy_valid_desired(root, "baseline")
            current_root = copy_valid_desired(root, "current")
            fleet_path = current_root / "environments" / "develop" / "fleets" / "develop-fleet-eu.yml"
            fleet = load_yaml(fleet_path)
            fleet["spec"]["bridges"] = []
            save_yaml(fleet_path, fleet)

            baseline = validate_environment(REPO_ROOT, "develop", desired_root=baseline_root)
            current = validate_environment(REPO_ROOT, "develop", desired_root=current_root)
            plan = build_impact_plan(current, baseline)

        self.assertTrue(plan.destructive)
        self.assertIn("BRIDGE_REMOVED", {change["type"] for change in plan.changes})
        self.assertIn("develop-entry-nl-01", plan.affected["node_runtime"])

    def test_logical_node_removal_is_destructive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            baseline_root = copy_valid_desired(root, "baseline")
            current_root = copy_valid_desired(root, "current")
            environment = current_root / "environments" / "develop"
            (environment / "nodes" / "develop-exit-de.yml").unlink()
            (environment / "instances" / "develop-exit-de-01.yml").unlink()
            fleet_path = environment / "fleets" / "develop-fleet-eu.yml"
            fleet = load_yaml(fleet_path)
            fleet["spec"]["exits"] = []
            fleet["spec"]["bridges"] = []
            save_yaml(fleet_path, fleet)

            baseline = validate_environment(REPO_ROOT, "develop", desired_root=baseline_root)
            current = validate_environment(REPO_ROOT, "develop", desired_root=current_root)
            plan = build_impact_plan(current, baseline)

        self.assertTrue(plan.destructive)
        self.assertIn("LOGICAL_NODE_REMOVED", {change["type"] for change in plan.changes})
        self.assertIn("develop-exit-de-01", plan.affected["retire"])

    def test_previously_accepted_fleet_cannot_disappear(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            baseline_root = copy_valid_desired(root, "baseline")
            current_root = copy_valid_desired(root, "current")
            environment = current_root / "environments" / "develop"
            (environment / "fleets" / "develop-fleet-eu.yml").unlink()
            (environment / "nodes" / "develop-entry-nl.yml").unlink()
            (environment / "nodes" / "develop-exit-de.yml").unlink()
            (environment / "instances" / "develop-entry-nl-01.yml").unlink()
            (environment / "instances" / "develop-exit-de-01.yml").unlink()

            baseline = validate_environment(REPO_ROOT, "develop", desired_root=baseline_root)
            current = validate_environment(REPO_ROOT, "develop", desired_root=current_root)
            with self.assertRaises(PlanningError):
                build_impact_plan(current, baseline)


if __name__ == "__main__":
    unittest.main()
