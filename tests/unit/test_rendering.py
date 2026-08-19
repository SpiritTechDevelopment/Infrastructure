from __future__ import annotations

import ipaddress
import json
import shutil
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import yaml

from fleetctl.adapters import OutputDirectoryError, write_rendered_files
from fleetctl.compiler import (
    MonitoringPlanError,
    compile_monitoring_targets,
    render_files,
)
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
        self.assertIn("bootstrap-inventory.json", files)
        self.assertIn("control-plan.json", files)

    def test_control_plan_pins_backend_migrations_and_postgres(self) -> None:
        plan = json.loads(render_files(self.state)["control-plan.json"])
        self.assertEqual(plan["environment"], "develop")
        self.assertEqual(plan["network"]["management_address"], "10.80.0.1")
        self.assertEqual(plan["network"]["backend_host_port"], 9443)
        self.assertEqual(plan["backend"]["source_git_sha"], "a" * 40)
        self.assertIn("@sha256:", plan["backend"]["image"])
        self.assertIn("@sha256:", plan["backend"]["migration_image"])
        self.assertIn("@sha256:", plan["postgres"]["image"])
        self.assertTrue(
            all(reference.startswith("secret://") for reference in plan["secret_refs"].values())
        )

    def test_control_plan_projects_the_bot_beside_the_backend(self) -> None:
        plan = json.loads(render_files(self.state)["control-plan.json"])
        bot = plan["bot"]
        self.assertEqual(bot["source_git_sha"], "b" * 40)
        self.assertIn("@sha256:", bot["image"])
        self.assertIn("@sha256:", bot["ingress"]["tunnel_image"])
        # Своя база и свои роли внутри общего инстанса.
        self.assertNotEqual(bot["postgres"]["database"], plan["postgres"]["database"])
        self.assertNotEqual(bot["postgres"]["owner_user"], plan["postgres"]["owner_user"])
        # Номер флота берётся из реестра, а не из второй копии в настройках.
        self.assertEqual(bot["settings"]["friends_plan_fleet_id"], 1)
        # Оба публичных URL выводятся из одного объявленного имени.
        self.assertEqual(bot["settings"]["subscription_base_url"], "https://bot.develop.example.invalid")
        self.assertEqual(
            bot["settings"]["mini_app_url"], bot["settings"]["subscription_base_url"]
        )
        # Бэкенд адресуется тем именем, которое несёт его серверный
        # сертификат, — иначе TLS не сойдётся.
        self.assertEqual(bot["network"]["backend_target"], plan["network"]["backend_endpoint"])

    def test_control_plan_leaves_the_bot_null_when_none_is_declared(self) -> None:
        """Отсутствие бота — это null, а не пустая заглушка.

        Роль включает свои задачи по `control_plan.bot`; заглушка вместо null
        включила бы их в среде, которая бота не разворачивает, и выкатка
        бэкенда споткнулась бы о ненастроенного соседа.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            desired_root = Path(temporary_directory) / "desired"
            shutil.copytree(VALID_DESIRED, desired_root)
            target = desired_root / "environments" / "develop" / "environment.yml"
            document = yaml.safe_load(target.read_text(encoding="utf-8"))
            del document["spec"]["control"]["bot"]
            target.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
            state = validate_environment(REPO_ROOT, "develop", desired_root=desired_root)
        plan = json.loads(render_files(state)["control-plan.json"])
        self.assertIsNone(plan["bot"])
        # Бэкенд при этом проецируется полностью: бот необязателен, а не
        # обязателен-но-пустой.
        self.assertIn("@sha256:", plan["backend"]["image"])

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
        # Two instances contribute five targets each; the control plane adds
        # two that belong to no fleet instance — the backend and the database.
        self.assertEqual(len(plan["targets"]), 12)
        entry_targets = [
            target
            for target in plan["targets"]
            if target["labels"]["instance"] == "develop-entry-nl-01"
        ]
        self.assertEqual(
            {target["service"] for target in entry_targets},
            {"agent", "agent-metrics", "node-exporter", "public-vless", "xray-metrics"},
        )
        self.assertTrue(all(target["slo_eligible"] for target in entry_targets))
        self.assertTrue(all(target["labels"]["lifecycle"] == "serving" for target in entry_targets))
        agent = next(target for target in entry_targets if target["service"] == "agent")
        self.assertEqual(agent["endpoint"]["address"], "10.80.1.11")
        self.assertEqual(agent["endpoint"]["port"], 9443)

    def test_scraped_metrics_targets_only_ever_use_the_management_overlay(self) -> None:
        plan = json.loads(render_files(self.state)["monitoring-targets.json"])
        scraped = [
            target
            for target in plan["targets"]
            if target["kind"] == "metrics" and target["collection"] == "management"
        ]
        self.assertEqual(
            {target["id"] for target in scraped},
            {
                "control-develop:backend-metrics",
                "control-develop:postgres-metrics",
                "develop-entry-nl-01:agent-metrics",
                "develop-entry-nl-01:node-exporter",
                "develop-exit-de-01:agent-metrics",
                "develop-exit-de-01:node-exporter",
            },
        )
        # A scrape target that resolved to loopback would be unreachable from
        # the management host and would silently monitor nothing.
        management_network = ipaddress.ip_network("10.80.0.0/16")
        for target in scraped:
            address = ipaddress.ip_address(target["endpoint"]["address"])
            self.assertIn(address, management_network, target["id"])
        self.assertEqual(
            next(
                target["endpoint"]
                for target in scraped
                if target["id"] == "control-develop:backend-metrics"
            ),
            {"scheme": "http", "address": "10.80.0.1", "port": 8080, "path": "/metrics"},
        )

    def test_scrape_target_outside_the_overlay_is_refused(self) -> None:
        # Only reachable if the address derivation itself regresses, which is
        # exactly when a silent public metrics endpoint would be introduced.
        with unittest.mock.patch(
            "fleetctl.compiler.monitoring.control_management_address",
            return_value="203.0.113.7",
        ):
            with self.assertRaises(MonitoringPlanError) as caught:
                compile_monitoring_targets(self.state)
        self.assertIn("203.0.113.7", str(caught.exception))
        self.assertIn("outside the management network", str(caught.exception))

    def test_xray_expvar_stays_node_local_and_unscraped(self) -> None:
        plan = json.loads(render_files(self.state)["monitoring-targets.json"])
        xray = next(
            target for target in plan["targets"] if target["id"] == "develop-entry-nl-01:xray-metrics"
        )
        self.assertEqual(xray["collection"], "node-local")
        self.assertEqual(xray["endpoint"]["address"], "127.0.0.1")
        self.assertEqual(xray["endpoint"]["path"], "/debug/vars")

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
        self.assertEqual(len(candidate_targets), 5)
        self.assertTrue(all(not target["slo_eligible"] for target in candidate_targets))
        self.assertTrue(all(target["readiness_expected"] for target in candidate_targets))

    def test_unassigned_decommission_node_remains_monitored_but_not_slo_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            desired_root = Path(temporary_directory) / "desired"
            shutil.copytree(VALID_DESIRED, desired_root)
            fleet_path = (
                desired_root
                / "environments"
                / "develop"
                / "fleets"
                / "develop-fleet-eu.yml"
            )
            fleet = yaml.safe_load(fleet_path.read_text(encoding="utf-8"))
            fleet["spec"]["entries"] = []
            fleet["spec"]["bridges"] = []
            fleet_path.write_text(yaml.safe_dump(fleet, sort_keys=False), encoding="utf-8")
            state = validate_environment(REPO_ROOT, "develop", desired_root=desired_root)
            files = render_files(state)
            monitoring = json.loads(files["monitoring-targets.json"])
            node_plan = json.loads(files["node-plans/develop-entry-nl-01.json"])

        entry_targets = [
            target
            for target in monitoring["targets"]
            if target["labels"]["logical_node"] == "develop-entry-nl"
        ]
        self.assertEqual(len(entry_targets), 5)
        self.assertTrue(all(target["labels"]["fleet"] == "unassigned" for target in entry_targets))
        self.assertTrue(all(not target["slo_eligible"] for target in entry_targets))
        self.assertTrue(all(target["readiness_expected"] for target in entry_targets))
        self.assertEqual(node_plan["fleet"], {"id": None, "vpn_fleet_id": None})
        self.assertEqual(node_plan["routing"]["bridges_as_entry"], [])
        self.assertEqual(node_plan["routing"]["bridges_as_exit"], [])

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
            entry_hosts["develop-entry-nl-01"]["spiritvpn_node_plan_file"],
            "node-plans/develop-entry-nl-01.json",
        )
        self.assertEqual(
            set(entry_hosts["develop-entry-nl-01"]),
            {"ansible_host", "ansible_port", "spiritvpn_node_plan_file"},
        )

    def test_inventory_connects_on_the_declared_ssh_port(self) -> None:
        files = render_files(self.state)
        inventory = json.loads(files["ansible-inventory.json"])
        groups = inventory["all"]["children"]["spiritvpn_fleet"]["children"]
        entry = groups["entry"]["hosts"]["develop-entry-nl-01"]
        plan = json.loads(files["node-plans/develop-entry-nl-01.json"])
        self.assertEqual(entry["ansible_port"], 232)
        self.assertEqual(plan["infrastructure"]["networking"]["ssh"]["port"], 232)

    def test_bootstrap_inventory_uses_public_address_and_same_node_plan(self) -> None:
        inventory = json.loads(render_files(self.state)["bootstrap-inventory.json"])
        hosts = inventory["all"]["children"]["spiritvpn_bootstrap"]["hosts"]
        entry = hosts["develop-entry-nl-01"]
        self.assertEqual(entry["ansible_host"], "192.0.2.10")
        self.assertEqual(entry["ansible_user"], "root")
        self.assertEqual(entry["spiritvpn_connection_phase"], "bootstrap")
        self.assertEqual(entry["spiritvpn_node_plan_file"], "node-plans/develop-entry-nl-01.json")

    def test_bootstrap_inventory_keeps_the_default_ssh_port(self) -> None:
        # A clean VPS answers on 22 only; the compiled sshd port becomes reachable
        # after the common role has moved it, never before.
        inventory = json.loads(render_files(self.state)["bootstrap-inventory.json"])
        hosts = inventory["all"]["children"]["spiritvpn_bootstrap"]["hosts"]
        self.assertNotIn("ansible_port", hosts["develop-entry-nl-01"])

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
        self.assertEqual(
            plan["logical_node"]["mask"]["certificate_ref"],
            "secret://kv/develop/nodes/develop-entry-nl/mask#fullchain",
        )
        self.assertEqual(
            bridge["service_credential_ref"],
            "secret://kv/develop/bridges/develop-entry-nl.to-develop-exit-de#service_uuid",
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

        node_plan = json.loads(files["node-plans/develop-entry-nl-01.json"])
        self.assertEqual(node_plan["infrastructure"]["components"]["xray"]["digest"], "sha256:" + "f" * 64)
        self.assertEqual(node_plan["instance"]["agent"]["endpoint"], "10.80.1.11:9555")
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
