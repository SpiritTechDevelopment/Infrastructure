from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# Свой каталог на пути явно: тесты запускаются и через `unittest discover`,
# и как `tests.unit.<модуль>`, и во втором случае соседний модуль иначе не
# находится.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import topology_fixture

import yaml

from fleetctl.compiler import (
    BackendManifestError,
    backend_manifest_bytes,
    backend_manifest_payload_digest,
    compile_backend_manifest,
)
from fleetctl.planning import build_impact_plan, build_initial_baseline
from fleetctl.validation import validate_environment


REPO_ROOT = Path(__file__).resolve().parents[2]
VALID_DESIRED = REPO_ROOT / "tests" / "fixtures" / "valid" / "desired"


class BackendManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.state = validate_environment(REPO_ROOT, "develop", desired_root=VALID_DESIRED)
        cls.initial_plan = build_impact_plan(
            cls.state,
            build_initial_baseline(cls.state),
            initial_deployment=True,
        )

    def compile_initial(self, revision: int = 42) -> dict[str, object]:
        return compile_backend_manifest(
            self.state,
            self.initial_plan,
            revision=revision,
            allow_destructive=False,
        )

    def test_manifest_egress_tags_are_the_tags_the_entry_node_actually_has(self) -> None:
        # The backend hands egress_tag to the agent verbatim as User.egress_key,
        # and the agent writes it onto a user in Xray. Two compilers derive that
        # string independently: this one and node_plans. If they ever disagree,
        # every side stays internally consistent, no gate notices, and customer
        # traffic is routed to an outbound the node does not have.
        from fleetctl.compiler import render_files

        manifest = self.compile_initial()
        files = render_files(self.state)
        bridges = [
            bridge for fleet in manifest["fleets"] for bridge in fleet["bridges"]
        ]
        self.assertTrue(bridges, "the fixture must exercise at least one bridge")

        for bridge in bridges:
            entry_plans = [
                json.loads(payload)
                for name, payload in files.items()
                if name.startswith("node-plans/")
                and json.loads(payload)["logical_node"]["id"] == bridge["entry_node_id"]
            ]
            self.assertTrue(
                entry_plans,
                f"no compiled plan for entry node {bridge['entry_node_id']}",
            )
            for plan in entry_plans:
                routing = plan["routing"]
                self.assertIn(
                    bridge["egress_tag"],
                    routing["egress_table"],
                    f"{plan['instance']['id']} has no outbound for {bridge['egress_tag']}",
                )
                self.assertEqual(
                    {
                        item["routing_key"]: item["egress_tag"]
                        for item in routing["bridges_as_entry"]
                        if item["routing_key"] == bridge["routing_key"]
                    },
                    {bridge["routing_key"]: bridge["egress_tag"]},
                )

    def test_contract_is_pinned_byte_for_byte(self) -> None:
        proto = REPO_ROOT / "contracts" / "manifest" / "v1" / "manifest.proto"
        self.assertEqual(
            hashlib.sha256(proto.read_bytes()).hexdigest(),
            "bbbe8b19780187eac043eb124609df112e6c863d9009dd2de0036bc328b67ce9",
        )

    def test_compiles_complete_v1_request(self) -> None:
        request = self.compile_initial()

        self.assertEqual(request["schema_version"], 1)
        self.assertEqual(request["revision"], 42)
        self.assertFalse(request["allow_destructive"])
        self.assertEqual(
            [node["node_id"] for node in request["nodes"]],
            ["develop-entry-nl", "develop-exit-de"],
        )
        entry = request["nodes"][0]
        self.assertEqual(entry["agent"]["endpoint"], "10.80.1.11:9443")
        self.assertEqual(
            entry["agent"]["tls_server_name"],
            "develop-entry-nl-01.agent.develop.internal",
        )
        self.assertEqual(
            entry["agent"]["certificate_identity"],
            "spiffe://spiritvpn/develop/instance/develop-entry-nl-01",
        )
        self.assertEqual(entry["public"]["address"], "edge-a7.develop.example.invalid")
        fleet = request["fleets"][0]
        self.assertEqual(fleet["vpn_fleet_id"], 1)
        self.assertEqual(fleet["node_ids"], ["develop-entry-nl", "develop-exit-de"])
        self.assertEqual(fleet["bridges"][0]["egress_tag"], "xo-develop-exit-de")

    def test_manifest_never_contains_private_secret_references(self) -> None:
        encoded = backend_manifest_bytes(self.compile_initial()).decode("utf-8")
        self.assertNotIn("secret://", encoded)
        self.assertNotIn("private_key", encoded)
        self.assertNotIn("service_uuid", encoded)

    def test_serialization_and_payload_digest_are_deterministic(self) -> None:
        first = self.compile_initial(revision=42)
        second = self.compile_initial(revision=42)
        next_revision = self.compile_initial(revision=43)

        self.assertEqual(backend_manifest_bytes(first), backend_manifest_bytes(second))
        self.assertEqual(
            backend_manifest_payload_digest(first),
            backend_manifest_payload_digest(next_revision),
        )

    def test_revision_must_be_positive_uint64(self) -> None:
        for revision in (0, -1, True, 2**64):
            with self.subTest(revision=revision):
                with self.assertRaises(BackendManifestError):
                    self.compile_initial(revision=revision)

    def test_excess_destructive_permission_is_refused(self) -> None:
        with self.assertRaisesRegex(BackendManifestError, "non-destructive"):
            compile_backend_manifest(
                self.state,
                self.initial_plan,
                revision=42,
                allow_destructive=True,
            )

    def test_destructive_plan_requires_explicit_permission(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            desired_root = Path(temporary_directory) / "desired"
            shutil.copytree(VALID_DESIRED, desired_root)
            fleet = topology_fixture.get(desired_root, "develop-fleet-eu")
            fleet["spec"]["entries"] = []
            fleet["spec"]["bridges"] = []
            topology_fixture.put(desired_root, fleet)
            current = validate_environment(REPO_ROOT, "develop", desired_root=desired_root)

        plan = build_impact_plan(current, self.state)
        self.assertTrue(plan.destructive)
        with self.assertRaisesRegex(BackendManifestError, "explicit"):
            compile_backend_manifest(current, plan, revision=43, allow_destructive=False)

        request = compile_backend_manifest(current, plan, revision=43, allow_destructive=True)
        self.assertTrue(request["allow_destructive"])
        self.assertEqual(request["fleets"][0]["node_ids"], ["develop-exit-de"])
        self.assertEqual(
            [node["node_id"] for node in request["nodes"]],
            ["develop-entry-nl", "develop-exit-de"],
        )

    def test_plan_must_match_rendered_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            desired_root = Path(temporary_directory) / "desired"
            shutil.copytree(VALID_DESIRED, desired_root)
            node = topology_fixture.get(desired_root, "develop-entry-nl")
            node["spec"]["display_name"] = "Changed after planning"
            topology_fixture.put(desired_root, node)
            changed = validate_environment(REPO_ROOT, "develop", desired_root=desired_root)

        with self.assertRaisesRegex(BackendManifestError, "does not describe"):
            compile_backend_manifest(
                changed,
                self.initial_plan,
                revision=42,
                allow_destructive=False,
            )

    def test_rendered_artifact_is_valid_json(self) -> None:
        request = self.compile_initial()
        self.assertEqual(json.loads(backend_manifest_bytes(request)), request)


if __name__ == "__main__":
    unittest.main()
