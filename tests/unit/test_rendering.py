from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from fleetctl.adapters import OutputDirectoryError, write_rendered_files
from fleetctl.compiler import render_files
from fleetctl.validation import validate_environment


REPO_ROOT = Path(__file__).resolve().parents[2]
VALID_DESIRED = REPO_ROOT / "tests" / "fixtures" / "valid" / "desired"


class RenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.state = validate_environment(REPO_ROOT, "develop", desired_root=VALID_DESIRED)

    def test_render_is_byte_for_byte_deterministic(self) -> None:
        self.assertEqual(render_files(self.state), render_files(self.state))

    def test_render_contains_dns_and_monitoring_artifacts(self) -> None:
        files = render_files(self.state)
        self.assertIn("dns-plan.json", files)
        self.assertIn("monitoring-targets.json", files)

    def test_dns_plan_publishes_only_serving_entries(self) -> None:
        plan = json.loads(render_files(self.state)["dns-plan.json"])
        self.assertEqual(plan["zone"], "develop.example.invalid")
        self.assertEqual(len(plan["records"]), 1)
        record = plan["records"][0]
        self.assertEqual(record["logical_node_id"], "develop-entry-nl")
        self.assertEqual(record["instance_id"], "develop-entry-nl-01")
        self.assertEqual(record["name"], "edge-a7.develop.example.invalid")
        self.assertEqual(record["record_type"], "A")
        self.assertEqual(record["value"], "192.0.2.10")
        self.assertEqual(record["ttl_seconds"], 60)
        self.assertFalse(record["proxied"])

    def test_monitoring_targets_distinguish_instance_lifecycle_and_slo(self) -> None:
        plan = json.loads(render_files(self.state)["monitoring-targets.json"])
        self.assertEqual(len(plan["targets"]), 8)
        entry_targets = [
            target
            for target in plan["targets"]
            if target["labels"]["instance"] == "develop-entry-nl-01"
        ]
        self.assertEqual(
            {target["service"] for target in entry_targets},
            {"agent", "node-exporter", "public-vless", "xray-metrics"},
        )
        self.assertTrue(all(target["slo_eligible"] for target in entry_targets))
        self.assertTrue(all(target["labels"]["lifecycle"] == "serving" for target in entry_targets))
        agent = next(target for target in entry_targets if target["service"] == "agent")
        self.assertEqual(agent["endpoint"]["address"], "10.80.1.11")
        self.assertEqual(agent["endpoint"]["port"], 9443)

    def test_candidate_is_monitored_but_not_published_or_slo_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            desired_root = Path(temporary_directory) / "desired"
            shutil.copytree(VALID_DESIRED, desired_root)
            instances = desired_root / "environments" / "develop" / "instances"
            candidate = yaml.safe_load(
                (instances / "develop-entry-nl-01.yml").read_text(encoding="utf-8")
            )
            candidate["metadata"]["id"] = "develop-entry-nl-02"
            candidate["spec"]["target_state"] = "candidate"
            candidate["spec"]["public_address"] = "192.0.2.11"
            candidate["spec"]["provider"]["resource_id"] = "fixture-entry-02"
            (instances / "develop-entry-nl-02.yml").write_text(
                yaml.safe_dump(candidate, sort_keys=False),
                encoding="utf-8",
            )
            state = validate_environment(REPO_ROOT, "develop", desired_root=desired_root)
            files = render_files(state)

        dns = json.loads(files["dns-plan.json"])
        self.assertEqual([record["instance_id"] for record in dns["records"]], ["develop-entry-nl-01"])
        monitoring = json.loads(files["monitoring-targets.json"])
        candidate_targets = [
            target
            for target in monitoring["targets"]
            if target["labels"]["instance"] == "develop-entry-nl-02"
        ]
        self.assertEqual(len(candidate_targets), 4)
        self.assertTrue(all(not target["slo_eligible"] for target in candidate_targets))
        self.assertTrue(all(target["readiness_expected"] for target in candidate_targets))

    def test_inventory_contains_derived_management_addresses(self) -> None:
        inventory = json.loads(render_files(self.state)["ansible-inventory.json"])
        groups = inventory["all"]["children"]["spiritvpn_fleet"]["children"]
        entry_hosts = groups["entry"]["hosts"]
        exit_hosts = groups["exit"]["hosts"]
        self.assertEqual(entry_hosts["develop-entry-nl-01"]["ansible_host"], "10.80.1.11")
        self.assertEqual(exit_hosts["develop-exit-de-01"]["ansible_host"], "10.80.2.11")
        self.assertEqual(list(entry_hosts), ["develop-entry-nl-01"])
        self.assertEqual(list(exit_hosts), ["develop-exit-de-01"])
        self.assertEqual(
            entry_hosts["develop-entry-nl-01"]["spiritvpn_agent_endpoint"],
            "10.80.1.11:9443",
        )
        self.assertEqual(entry_hosts["develop-entry-nl-01"]["spiritvpn_management_mtu"], 1420)
        self.assertEqual(entry_hosts["develop-entry-nl-01"]["node_limits_egress_limit_mbps"], 900)
        self.assertEqual(
            entry_hosts["develop-entry-nl-01"]["node_limits_flow_isolation"],
            "dual-dsthost",
        )
        self.assertEqual(exit_hosts["develop-exit-de-01"]["node_limits_flow_isolation"], "flows")

    def test_entry_plan_contains_stable_logical_exit_projection(self) -> None:
        files = render_files(self.state)
        plan = json.loads(files["node-plans/develop-entry-nl-01.json"])
        bridge = plan["routing"]["bridges_as_entry"][0]
        self.assertEqual(bridge["egress_tag"], "xo-develop-exit-de")
        self.assertEqual(bridge["target"]["instance_id"], "develop-exit-de-01")
        self.assertEqual(bridge["target"]["address"], "192.0.2.20")
        self.assertEqual(
            plan["logical_node"]["reality"]["private_key_ref"],
            "secret://kv/develop/nodes/develop-entry-nl/reality#private_key",
        )
        self.assertEqual(plan["instance"]["bandwidth"]["egress_limit_mbps"], 900)
        self.assertEqual(plan["instance"]["bandwidth"]["qdisc"]["flow_isolation"], "dual-dsthost")
        self.assertEqual(
            plan["infrastructure"]["components"]["xray"]["digest"],
            "sha256:0000000000000000000000000000000000000000000000000000000000000001",
        )

    def test_node_override_is_used_by_every_node_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            desired_root = Path(temporary_directory) / "desired"
            shutil.copytree(VALID_DESIRED, desired_root)
            node_path = desired_root / "environments" / "develop" / "nodes" / "develop-entry-nl.yml"
            node = yaml.safe_load(node_path.read_text(encoding="utf-8"))
            node["spec"]["common_overrides"] = {
                "components": {
                    "xray": {"digest": "sha256:" + "f" * 64},
                },
                "networking": {"agent": {"port": 9555}},
                "observability": {"ports": {"node_exporter": 9200}},
            }
            node_path.write_text(yaml.safe_dump(node, sort_keys=False), encoding="utf-8")
            state = validate_environment(REPO_ROOT, "develop", desired_root=desired_root)
            files = render_files(state)

        inventory = json.loads(files["ansible-inventory.json"])
        groups = inventory["all"]["children"]["spiritvpn_fleet"]["children"]
        self.assertEqual(
            groups["entry"]["hosts"]["develop-entry-nl-01"]["spiritvpn_agent_endpoint"],
            "10.80.1.11:9555",
        )
        self.assertEqual(
            groups["exit"]["hosts"]["develop-exit-de-01"]["spiritvpn_agent_endpoint"],
            "10.80.2.11:9443",
        )
        node_plan = json.loads(files["node-plans/develop-entry-nl-01.json"])
        self.assertEqual(node_plan["infrastructure"]["components"]["xray"]["digest"], "sha256:" + "f" * 64)
        monitoring = json.loads(files["monitoring-targets.json"])
        entry_exporter = next(
            target
            for target in monitoring["targets"]
            if target["id"] == "develop-entry-nl-01:node-exporter"
        )
        exit_exporter = next(
            target
            for target in monitoring["targets"]
            if target["id"] == "develop-exit-de-01:node-exporter"
        )
        self.assertEqual(entry_exporter["endpoint"]["port"], 9200)
        self.assertEqual(exit_exporter["endpoint"]["port"], 9100)

    def test_writer_replaces_only_marked_output_and_removes_stale_files(self) -> None:
        files = render_files(self.state)
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "rendered"
            write_rendered_files(output, files)
            stale = output / "node-plans" / "stale.json"
            stale.write_text("stale", encoding="utf-8")
            write_rendered_files(output, files)
            self.assertFalse(stale.exists())
            self.assertTrue((output / ".fleetctl-output").is_file())

    def test_writer_refuses_unmarked_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "existing"
            output.mkdir()
            with self.assertRaises(OutputDirectoryError):
                write_rendered_files(output, render_files(self.state))


if __name__ == "__main__":
    unittest.main()
