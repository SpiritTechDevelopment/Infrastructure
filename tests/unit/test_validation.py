from __future__ import annotations

import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from fleetctl.cli import main
from fleetctl.validation import DesiredStateInvalid, validate_environment


REPO_ROOT = Path(__file__).resolve().parents[2]


class DesiredStateValidationTests(unittest.TestCase):
    def validate_mutated_fixture(self, relative_path: str, mutate: object) -> set[str]:
        source = REPO_ROOT / "tests" / "fixtures" / "valid" / "desired"
        with tempfile.TemporaryDirectory() as temporary_directory:
            desired_root = Path(temporary_directory) / "desired"
            shutil.copytree(source, desired_root)
            target = desired_root / relative_path
            document = yaml.safe_load(target.read_text(encoding="utf-8"))
            mutate(document)
            target.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
            with self.assertRaises(DesiredStateInvalid) as raised:
                validate_environment(REPO_ROOT, "develop", desired_root=desired_root)
            return {issue.code for issue in raised.exception.issues}

    def test_repository_environments_are_valid(self) -> None:
        # develop declares the live fleet: one entry in Russia and two exits it
        # bridges to. prod is still an empty placeholder, so the assertion that
        # both environments validate has to carry two different shapes.
        for environment, fleets in (("develop", 1), ("prod", 0)):
            with self.subTest(environment=environment):
                state = validate_environment(REPO_ROOT, environment)
                self.assertEqual(state.environment.object_id, environment)
                self.assertEqual(len(state.fleets), fleets)

    def test_develop_declares_the_live_fleet(self) -> None:
        state = validate_environment(REPO_ROOT, "develop")
        self.assertEqual(
            [node.object_id for node in state.nodes],
            ["develop-entry-ru", "develop-exit-nl", "develop-exit-ro"],
        )
        # Slots are unique per role, so the second exit is 02 rather than 01.
        self.assertEqual(
            [instance.object_id for instance in state.instances],
            ["develop-entry-ru-01", "develop-exit-nl-01", "develop-exit-ro-02"],
        )
        # The entry is what clients dial, so its public name has to be the one
        # that actually resolves; the exits are reached by address and carry
        # their names only as REALITY server names.
        entry = next(node for node in state.nodes if node.role == "entry")
        self.assertEqual(entry.hostname, state.environment.dns_zone)

    def test_staging_environment_is_not_supported(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["validate", "--environment", "staging"])
        self.assertFalse((REPO_ROOT / "desired" / "environments" / "staging").exists())

    def test_complete_fixture_is_valid(self) -> None:
        desired_root = REPO_ROOT / "tests" / "fixtures" / "valid" / "desired"
        state = validate_environment(REPO_ROOT, "develop", desired_root=desired_root)
        self.assertEqual(len(state.fleets), 1)
        self.assertEqual(len(state.nodes), 2)
        self.assertEqual(len(state.instances), 2)
        self.assertEqual(state.fleet_ids["develop-fleet-eu"], 1)
        profile = state.common.limits.bandwidth_profiles["vps-1g"]
        self.assertEqual(profile.egress_limit_mbps, 900)

    def test_node_without_fleet_membership_is_valid_for_decommission(self) -> None:
        source = REPO_ROOT / "tests" / "fixtures" / "valid" / "desired"
        with tempfile.TemporaryDirectory() as temporary_directory:
            desired_root = Path(temporary_directory) / "desired"
            shutil.copytree(source, desired_root)
            fleet_path = desired_root / "environments" / "develop" / "fleets" / "develop-fleet-eu.yml"
            fleet = yaml.safe_load(fleet_path.read_text(encoding="utf-8"))
            fleet["spec"]["entries"] = []
            fleet["spec"]["bridges"] = []
            fleet_path.write_text(yaml.safe_dump(fleet, sort_keys=False), encoding="utf-8")

            state = validate_environment(REPO_ROOT, "develop", desired_root=desired_root)

        self.assertEqual(len(state.nodes), 2)
        self.assertEqual(state.fleets[0].entries, ())

    def test_fleet_id_must_fit_positive_signed_int64(self) -> None:
        for value in (0, -1, 2**63):
            with self.subTest(value=value):
                def mutate(document: dict[str, object], fleet_id: int = value) -> None:
                    document["develop-fleet-eu"] = fleet_id

                codes = self.validate_mutated_fixture("fleet-ids.yml", mutate)
                self.assertIn("FLEET_ID_VALUE", codes)

    def test_common_overrides_follow_common_environment_node_precedence(self) -> None:
        source = REPO_ROOT / "tests" / "fixtures" / "valid" / "desired"
        with tempfile.TemporaryDirectory() as temporary_directory:
            desired_root = Path(temporary_directory) / "desired"
            shutil.copytree(source, desired_root)
            environment_path = desired_root / "environments" / "develop" / "environment.yml"
            environment = yaml.safe_load(environment_path.read_text(encoding="utf-8"))
            environment["spec"]["common_overrides"] = {
                "networking": {"agent": {"port": 9555}},
            }
            environment_path.write_text(yaml.safe_dump(environment, sort_keys=False), encoding="utf-8")
            node_path = desired_root / "environments" / "develop" / "nodes" / "develop-entry-nl.yml"
            node = yaml.safe_load(node_path.read_text(encoding="utf-8"))
            node["spec"]["common_overrides"] = {
                "networking": {"agent": {"port": 9666}},
                "limits": {"bandwidth_profiles": {"vps-1g": {"port_capacity_mbps": 2000}}},
            }
            node_path.write_text(yaml.safe_dump(node, sort_keys=False), encoding="utf-8")

            state = validate_environment(REPO_ROOT, "develop", desired_root=desired_root)

        self.assertEqual(state.common.networking.agent_port, 9443)
        self.assertEqual(state.environment_common.networking.agent_port, 9555)
        self.assertEqual(state.common_for_node("develop-exit-de").networking.agent_port, 9555)
        entry_common = state.common_for_node("develop-entry-nl")
        self.assertEqual(entry_common.networking.agent_port, 9666)
        self.assertEqual(entry_common.limits.bandwidth_profiles["vps-1g"].egress_limit_mbps, 1800)

    def test_common_overrides_reject_unknown_fields(self) -> None:
        def mutate(document: dict[str, object]) -> None:
            document["spec"]["common_overrides"] = {"networking": {"agent": {"unknown": 1}}}

        codes = self.validate_mutated_fixture("environments/develop/environment.yml", mutate)
        self.assertIn("SCHEMA", codes)

    def test_common_overrides_still_obey_semantic_policy(self) -> None:
        def mutate(document: dict[str, object]) -> None:
            document["spec"]["common_overrides"] = {"networking": {"dns": {"proxied": True}}}

        codes = self.validate_mutated_fixture(
            "environments/develop/nodes/develop-entry-nl.yml",
            mutate,
        )
        self.assertIn("DNS_POLICY", codes)

    def test_partial_new_bandwidth_profile_is_rejected_without_crashing(self) -> None:
        def mutate(document: dict[str, object]) -> None:
            document["spec"]["common_overrides"] = {
                "limits": {"bandwidth_profiles": {"vps-2g": {"port_capacity_mbps": 2000}}}
            }

        codes = self.validate_mutated_fixture(
            "environments/develop/nodes/develop-entry-nl.yml",
            mutate,
        )
        self.assertIn("COMMON_OVERRIDE", codes)

    def test_traffic_nodes_require_immutable_component_digests(self) -> None:
        def mutate(document: dict[str, object]) -> None:
            document["components"]["xray"]["digest"] = None

        codes = self.validate_mutated_fixture("common/components.yml", mutate)
        self.assertIn("COMPONENT_DIGEST_REQUIRED", codes)

    def test_instance_must_reference_an_existing_bandwidth_profile(self) -> None:
        def mutate(document: dict[str, object]) -> None:
            document["spec"]["bandwidth_profile"] = "missing-profile"

        codes = self.validate_mutated_fixture(
            "environments/develop/instances/develop-entry-nl-01.yml",
            mutate,
        )
        self.assertIn("BANDWIDTH_PROFILE", codes)

    def test_bandwidth_profile_cannot_use_more_than_ninety_percent(self) -> None:
        def mutate(document: dict[str, object]) -> None:
            document["bandwidth_profiles"]["vps-1g"]["egress_utilization_percent"] = 91

        codes = self.validate_mutated_fixture("common/limits.yml", mutate)
        self.assertIn("SCHEMA", codes)

    def test_unreviewed_kernel_overrides_are_not_part_of_desired_state(self) -> None:
        def mutate(document: dict[str, object]) -> None:
            document["kernel"] = {"nf_conntrack_max": 1048576}

        codes = self.validate_mutated_fixture("common/limits.yml", mutate)
        self.assertIn("SCHEMA", codes)

    def test_instance_public_address_must_be_an_ip_address(self) -> None:
        def mutate(document: dict[str, object]) -> None:
            document["spec"]["public_address"] = "not-an-address"

        codes = self.validate_mutated_fixture(
            "environments/develop/instances/develop-entry-nl-01.yml",
            mutate,
        )
        self.assertIn("SCHEMA", codes)

    def test_entry_hostname_must_belong_to_environment_zone(self) -> None:
        def mutate(document: dict[str, object]) -> None:
            document["spec"]["public"]["hostname"] = "edge.example.net"

        codes = self.validate_mutated_fixture(
            "environments/develop/nodes/develop-entry-nl.yml",
            mutate,
        )
        self.assertIn("DNS_ZONE", codes)

    def test_common_files_reject_unknown_fields(self) -> None:
        def mutate(document: dict[str, object]) -> None:
            document["unexpected"] = True

        codes = self.validate_mutated_fixture("common/networking.yml", mutate)
        self.assertIn("SCHEMA", codes)

    def test_broken_reference_fixture_is_rejected(self) -> None:
        desired_root = REPO_ROOT / "tests" / "fixtures" / "invalid" / "broken-reference" / "desired"
        with self.assertRaises(DesiredStateInvalid) as raised:
            validate_environment(REPO_ROOT, "develop", desired_root=desired_root)
        codes = {issue.code for issue in raised.exception.issues}
        self.assertIn("FLEET_NODE", codes)

    def test_cross_environment_secret_is_rejected(self) -> None:
        def mutate(document: dict[str, object]) -> None:
            document["spec"]["reality"]["private_key_ref"] = (
                "secret://kv/prod/nodes/develop-entry-nl/reality#private_key"
            )

        codes = self.validate_mutated_fixture(
            "environments/develop/nodes/develop-entry-nl.yml",
            mutate,
        )
        self.assertIn("SECRET_ENV", codes)

    def test_cross_environment_mask_secret_is_rejected(self) -> None:
        def mutate(document: dict[str, object]) -> None:
            document["spec"]["mask"]["certificate_ref"] = (
                "secret://kv/prod/nodes/develop-entry-nl/mask#fullchain"
            )

        codes = self.validate_mutated_fixture(
            "environments/develop/nodes/develop-entry-nl.yml",
            mutate,
        )
        self.assertIn("SECRET_ENV", codes)

    def test_cross_environment_bridge_secret_is_rejected(self) -> None:
        def mutate(document: dict[str, object]) -> None:
            document["spec"]["bridges"][0]["service_credential_ref"] = (
                "secret://kv/prod/bridges/develop-entry-nl.to-develop-exit-de#service_uuid"
            )

        codes = self.validate_mutated_fixture(
            "environments/develop/fleets/develop-fleet-eu.yml",
            mutate,
        )
        self.assertIn("SECRET_ENV", codes)

    def test_plaintext_secret_shaped_field_is_rejected_by_schema(self) -> None:
        def mutate(document: dict[str, object]) -> None:
            reality = document["spec"]["reality"]
            reality["private_key"] = "must-not-be-here"

        codes = self.validate_mutated_fixture(
            "environments/develop/nodes/develop-entry-nl.yml",
            mutate,
        )
        self.assertIn("SCHEMA", codes)

    def test_second_serving_instance_is_rejected(self) -> None:
        source = REPO_ROOT / "tests" / "fixtures" / "valid" / "desired"
        with tempfile.TemporaryDirectory() as temporary_directory:
            desired_root = Path(temporary_directory) / "desired"
            shutil.copytree(source, desired_root)
            instances = desired_root / "environments" / "develop" / "instances"
            first = yaml.safe_load((instances / "develop-entry-nl-01.yml").read_text(encoding="utf-8"))
            first["metadata"]["id"] = "develop-entry-nl-02"
            first["spec"]["provider"]["resource_id"] = "fixture-entry-02"
            (instances / "develop-entry-nl-02.yml").write_text(
                yaml.safe_dump(first, sort_keys=False),
                encoding="utf-8",
            )
            with self.assertRaises(DesiredStateInvalid) as raised:
                validate_environment(REPO_ROOT, "develop", desired_root=desired_root)
            self.assertIn("SERVING_COUNT", {issue.code for issue in raised.exception.issues})

    def test_management_slot_is_unique_within_role(self) -> None:
        def mutate(document: dict[str, object]) -> None:
            document["spec"]["role"] = "entry"

        codes = self.validate_mutated_fixture(
            "environments/develop/nodes/develop-exit-de.yml",
            mutate,
        )
        self.assertIn("MANAGEMENT_COLLISION", codes)

    def test_filename_must_match_object_id(self) -> None:
        source = REPO_ROOT / "tests" / "fixtures" / "valid" / "desired"
        with tempfile.TemporaryDirectory() as temporary_directory:
            desired_root = Path(temporary_directory) / "desired"
            shutil.copytree(source, desired_root)
            nodes = desired_root / "environments" / "develop" / "nodes"
            (nodes / "develop-entry-nl.yml").rename(nodes / "wrong-name.yml")
            with self.assertRaises(DesiredStateInvalid) as raised:
                validate_environment(REPO_ROOT, "develop", desired_root=desired_root)
            self.assertIn("FILENAME", {issue.code for issue in raised.exception.issues})

    def test_cli_reports_success(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(["--root", str(REPO_ROOT), "validate", "--environment", "develop"])
        self.assertEqual(exit_code, 0)
        self.assertIn("develop: valid", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
