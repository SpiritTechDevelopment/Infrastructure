from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

# Свой каталог на пути явно: тесты запускаются и через `unittest discover`,
# и как `tests.unit.<модуль>`, и во втором случае соседний модуль иначе не
# находится.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import topology_fixture

import yaml

from fleetctl.cli import main
from fleetctl.compiler import compile_dns_plan, render_files
from fleetctl.validation import DesiredStateInvalid, validate_environment


REPO_ROOT = Path(__file__).resolve().parents[2]
LIVE_DESIRED_SKIP_REASON = "encrypted repository desired state requires a trusted SOPS identity"


class DesiredStateValidationTests(unittest.TestCase):
    @staticmethod
    def mutate_declaration(desired_root: Path, relative_path: str, mutate: object) -> None:
        """Правит объявление, где бы оно ни лежало.

        Объекты окружения переехали в один `topology.yml`, но обращаться к ним
        по прежнему пути — `environments/develop/nodes/develop-exit-de.yml` —
        удобнее и читается лучше, чем «третий элемент spec.objects». Путь здесь
        и превращается в поиск по виду и идентификатору: имя файла было именем
        объекта, поэтому отображение однозначно и обратимо.
        """
        target = desired_root / relative_path
        if target.exists():
            document = yaml.safe_load(target.read_text(encoding="utf-8"))
            mutate(document)
            target.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
            return

        parts = Path(relative_path).parts
        if parts[0] != "environments":
            raise AssertionError(f"нет такого объявления: {relative_path}")
        environment = parts[1]
        stem = Path(parts[-1]).stem
        wanted = environment if stem == "environment" else stem

        bundle_path = desired_root / "environments" / environment / "topology.yml"
        bundle = yaml.safe_load(bundle_path.read_text(encoding="utf-8"))
        for document in bundle["spec"]["objects"]:
            if document["metadata"]["id"] == wanted:
                mutate(document)
                break
        else:
            raise AssertionError(f"в бандле нет объекта {wanted!r}")
        bundle_path.write_text(yaml.safe_dump(bundle, sort_keys=False), encoding="utf-8")

    def validate_mutated_fixture(self, relative_path: str, mutate: object) -> set[str]:
        source = REPO_ROOT / "tests" / "fixtures" / "valid" / "desired"
        with tempfile.TemporaryDirectory() as temporary_directory:
            desired_root = Path(temporary_directory) / "desired"
            shutil.copytree(source, desired_root)
            self.mutate_declaration(desired_root, relative_path, mutate)
            with self.assertRaises(DesiredStateInvalid) as raised:
                validate_environment(REPO_ROOT, "develop", desired_root=desired_root)
            return {issue.code for issue in raised.exception.issues}

    @unittest.skipIf(os.environ.get("SPIRITVPN_SKIP_LIVE_DESIRED") == "1", LIVE_DESIRED_SKIP_REASON)
    def test_repository_environments_are_valid(self) -> None:
        # develop declares the live fleet: one entry in Russia and one exit it
        # bridges to. prod is still an empty placeholder, so the assertion that
        # both environments validate has to carry two different shapes.
        for environment, fleets in (("develop", 1), ("prod", 0)):
            with self.subTest(environment=environment):
                state = validate_environment(REPO_ROOT, environment)
                self.assertEqual(state.environment.object_id, environment)
                self.assertEqual(len(state.fleets), fleets)

    @unittest.skipIf(os.environ.get("SPIRITVPN_SKIP_LIVE_DESIRED") == "1", LIVE_DESIRED_SKIP_REASON)
    def test_develop_declares_the_live_fleet(self) -> None:
        state = validate_environment(REPO_ROOT, "develop")
        # The former Netherlands exit was retired on 2026-08-18.
        self.assertEqual(
            [node.object_id for node in state.nodes],
            ["develop-entry-ru", "develop-exit-ro"],
        )
        # Slots are unique per role. The entry replacement keeps the old 01 in
        # draining while the new 02 becomes serving; the remaining exit keeps
        # the 02 it was given when it was the second one. Renumbering would
        # rewrite management addresses and agent identities for no reason.
        self.assertEqual(
            [instance.object_id for instance in state.instances],
            ["develop-entry-ru-01", "develop-exit-ro-02", "develop-entry-ru-02"],
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

    def test_a_standalone_object_is_refused_rather_than_ignored(self) -> None:
        """Файл прежнего формата обязан ронять валидацию, а не выпадать молча.

        Раскладка по объектам снята, и без этого отказа `nodes/foo.yml`,
        оставшийся от неё или написанный по памяти, просто не читался бы никем:
        нода объявлена, в выкатку не попала, и заметить это можно только по её
        отсутствию на хостах.
        """
        source = REPO_ROOT / "tests" / "fixtures" / "valid" / "desired"
        with tempfile.TemporaryDirectory() as temporary_directory:
            desired_root = Path(temporary_directory) / "desired"
            shutil.copytree(source, desired_root)
            node = topology_fixture.get(desired_root, "develop-entry-nl")
            stray = desired_root / "environments" / "develop" / "nodes"
            stray.mkdir()
            (stray / "develop-entry-nl.yml").write_text(
                yaml.safe_dump(node, sort_keys=False), encoding="utf-8"
            )
            with self.assertRaises(DesiredStateInvalid) as raised:
                validate_environment(REPO_ROOT, "develop", desired_root=desired_root)

        self.assertIn("STANDALONE_OBJECT", {issue.code for issue in raised.exception.issues})

    def test_topology_bundle_validates_every_embedded_object(self) -> None:
        source = REPO_ROOT / "tests" / "fixtures" / "valid" / "desired"
        with tempfile.TemporaryDirectory() as temporary_directory:
            desired_root = Path(temporary_directory) / "desired"
            shutil.copytree(source, desired_root)
            target = topology_fixture.path(desired_root)
            topology = topology_fixture.load(desired_root)
            node = next(
                item for item in topology["spec"]["objects"] if item["kind"] == "LogicalNode"
            )
            node["spec"]["public"]["port"] = 70000
            target.write_text(yaml.safe_dump(topology, sort_keys=False), encoding="utf-8")
            with self.assertRaises(DesiredStateInvalid) as raised:
                validate_environment(REPO_ROOT, "develop", desired_root=desired_root)

        self.assertIn("SCHEMA", {issue.code for issue in raised.exception.issues})

    def test_sops_topology_is_decrypted_only_in_memory(self) -> None:
        source = REPO_ROOT / "tests" / "fixtures" / "valid" / "desired"
        plain = validate_environment(REPO_ROOT, "develop", desired_root=source)
        with tempfile.TemporaryDirectory() as temporary_directory:
            desired_root = Path(temporary_directory) / "desired"
            shutil.copytree(source, desired_root)
            target = topology_fixture.path(desired_root)
            plaintext = target.read_text(encoding="utf-8")
            encrypted_path = target.with_name("topology.sops.yml")
            target.rename(encrypted_path)
            encrypted_path.write_text(
                yaml.safe_dump(
                    {
                        "apiVersion": "spiritvpn.io/v1alpha1",
                        "kind": "EnvironmentTopology",
                        "metadata": {"id": "develop"},
                        "spec": "ENC[AES256_GCM,data:fixture]",
                        "sops": {"version": "3.9.4"},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            completed = subprocess.CompletedProcess(
                ["sops", "--decrypt", str(encrypted_path)],
                0,
                stdout=plaintext,
                stderr="",
            )
            with unittest.mock.patch(
                "fleetctl.validation.loader.subprocess.run",
                return_value=completed,
            ) as decrypt:
                bundled = validate_environment(REPO_ROOT, "develop", desired_root=desired_root)

        decrypt.assert_called_once_with(
            ["sops", "--decrypt", str(encrypted_path)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(render_files(bundled), render_files(plain))

    def test_sops_decryption_failure_is_fail_closed(self) -> None:
        source = REPO_ROOT / "tests" / "fixtures" / "valid" / "desired"
        with tempfile.TemporaryDirectory() as temporary_directory:
            desired_root = Path(temporary_directory) / "desired"
            shutil.copytree(source, desired_root)
            target = topology_fixture.path(desired_root)
            encrypted_path = target.with_name("topology.sops.yml")
            target.rename(encrypted_path)
            encrypted_path.write_text("sops: {}\n", encoding="utf-8")
            completed = subprocess.CompletedProcess(
                ["sops", "--decrypt", str(encrypted_path)],
                1,
                stdout="",
                stderr="age identity is unavailable",
            )
            with unittest.mock.patch(
                "fleetctl.validation.loader.subprocess.run",
                return_value=completed,
            ), self.assertRaises(DesiredStateInvalid) as raised:
                validate_environment(REPO_ROOT, "develop", desired_root=desired_root)

        self.assertEqual(
            {issue.code for issue in raised.exception.issues},
            {"ENVIRONMENT_COUNT", "SOPS_DECRYPT"},
        )

    def test_node_without_fleet_membership_is_valid_for_decommission(self) -> None:
        source = REPO_ROOT / "tests" / "fixtures" / "valid" / "desired"
        with tempfile.TemporaryDirectory() as temporary_directory:
            desired_root = Path(temporary_directory) / "desired"
            shutil.copytree(source, desired_root)
            fleet = topology_fixture.get(desired_root, "develop-fleet-eu")
            fleet["spec"]["entries"] = []
            fleet["spec"]["bridges"] = []
            topology_fixture.put(desired_root, fleet)

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
            environment = topology_fixture.get(desired_root, "develop")
            environment["spec"]["common_overrides"] = {
                "networking": {"agent": {"port": 9555}},
            }
            topology_fixture.put(desired_root, environment)
            node = topology_fixture.get(desired_root, "develop-entry-nl")
            node["spec"]["common_overrides"] = {
                "networking": {"agent": {"port": 9666}},
                "limits": {"bandwidth_profiles": {"vps-1g": {"port_capacity_mbps": 2000}}},
            }
            topology_fixture.put(desired_root, node)

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

    def test_external_backup_command_must_be_a_bounded_absolute_argv(self) -> None:
        def mutate(document: dict[str, object]) -> None:
            document["spec"]["control"]["postgres"]["external_backup_command_argv"] = [
                "relative-adapter",
                " untrimmed ",
            ]

        codes = self.validate_mutated_fixture("environments/develop/environment.yml", mutate)
        self.assertIn("CONTROL_BACKUP_COMMAND", codes)

    def test_required_backup_must_name_an_external_adapter(self) -> None:
        def mutate(document: dict[str, object]) -> None:
            document["spec"]["control"]["postgres"]["backup_required"] = True
            document["spec"]["control"]["postgres"]["external_backup_command_argv"] = []

        codes = self.validate_mutated_fixture("environments/develop/environment.yml", mutate)
        self.assertIn("CONTROL_BACKUP_COMMAND", codes)

    def test_bot_may_not_take_over_the_backend_database(self) -> None:
        """Один инстанс, два арендатора.

        Совпавшее имя базы не ломает ни схему, ни выкатку: она проходит, а
        миграции бота приезжают в схему бэкенда. Отказ обязан случиться на
        валидации, потому что дальше это уже потерянные данные.
        """

        def mutate(document: dict[str, object]) -> None:
            control = document["spec"]["control"]
            control["bot"]["postgres"]["database"] = control["postgres"]["database"]

        codes = self.validate_mutated_fixture("environments/develop/environment.yml", mutate)
        self.assertIn("BOT_DB_SHARED", codes)

    def test_bot_may_not_reuse_the_backend_postgres_roles(self) -> None:
        def mutate(document: dict[str, object]) -> None:
            control = document["spec"]["control"]
            control["bot"]["postgres"]["owner_user"] = control["postgres"]["owner_user"]

        codes = self.validate_mutated_fixture("environments/develop/environment.yml", mutate)
        self.assertIn("BOT_DB_ROLE_SHARED", codes)

    def test_bot_identity_must_be_authorised_by_the_backend(self) -> None:
        """Личность, которой бэкенд не доверяет, даёт бота, который стартует.

        Он поднимется, дозвонится и получит отказ на каждом вызове — то есть
        сломается в трафике, а не в выкатке.
        """

        def mutate(document: dict[str, object]) -> None:
            document["spec"]["control"]["bot"]["settings"]["client_identity"] = (
                "spiffe://spiritvpn/develop/service/stranger"
            )

        codes = self.validate_mutated_fixture("environments/develop/environment.yml", mutate)
        self.assertIn("BOT_IDENTITY_UNAUTHORISED", codes)

    def test_bot_free_plan_must_name_a_registered_fleet(self) -> None:
        def mutate(document: dict[str, object]) -> None:
            document["spec"]["control"]["bot"]["settings"]["friends_plan_fleet"] = "no-such-fleet"

        codes = self.validate_mutated_fixture("environments/develop/environment.yml", mutate)
        self.assertIn("BOT_FLEET_UNKNOWN", codes)

    def test_bot_release_must_be_pinned_by_digest(self) -> None:
        def mutate(document: dict[str, object]) -> None:
            document["spec"]["control"]["bot"]["release"]["image"]["digest"] = "latest"

        codes = self.validate_mutated_fixture("environments/develop/environment.yml", mutate)
        self.assertIn("SCHEMA", codes)

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

    def test_control_public_endpoint_must_be_declared_whole(self) -> None:
        def mutate(document: dict[str, object]) -> None:
            del document["spec"]["control"]["public_endpoint"]["address"]

        codes = self.validate_mutated_fixture("environments/develop/environment.yml", mutate)
        self.assertIn("SCHEMA", codes)

    def test_control_public_endpoint_address_must_be_an_ip_address(self) -> None:
        def mutate(document: dict[str, object]) -> None:
            document["spec"]["control"]["public_endpoint"]["address"] = "not-an-address"

        codes = self.validate_mutated_fixture("environments/develop/environment.yml", mutate)
        self.assertIn("CONTROL_PUBLIC_ENDPOINT", codes)

    def test_control_public_endpoint_hostname_must_belong_to_environment_zone(self) -> None:
        def mutate(document: dict[str, object]) -> None:
            document["spec"]["control"]["public_endpoint"]["hostname"] = "control.example.net"

        codes = self.validate_mutated_fixture("environments/develop/environment.yml", mutate)
        self.assertIn("CONTROL_PUBLIC_ENDPOINT", codes)

    def test_control_without_public_endpoint_stays_valid(self) -> None:
        # Поле необязательное намеренно: baseline прошлой выкатки его не
        # содержит, и план против неё обязан грузиться. Отдельный путь, а не
        # `validate_mutated_fixture`, — тот утверждает отказ.
        source = REPO_ROOT / "tests" / "fixtures" / "valid" / "desired"
        with tempfile.TemporaryDirectory() as temporary_directory:
            desired_root = Path(temporary_directory) / "desired"
            shutil.copytree(source, desired_root)
            topology_fixture.edit(
                desired_root,
                "develop",
                lambda document: document["spec"]["control"].pop("public_endpoint"),
            )
            state = validate_environment(REPO_ROOT, "develop", desired_root=desired_root)
        self.assertIsNone(state.environment.control.public_hostname)
        self.assertEqual(compile_dns_plan(state)["records"][0]["id"], "develop-entry-nl")

    def test_common_files_reject_unknown_fields(self) -> None:
        def mutate(document: dict[str, object]) -> None:
            document["unexpected"] = True

        codes = self.validate_mutated_fixture("common/networking.yml", mutate)
        self.assertIn("SCHEMA", codes)

    def test_ssh_port_must_be_declared(self) -> None:
        def mutate(document: dict[str, object]) -> None:
            del document["ssh"]

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
            first = topology_fixture.get(desired_root, "develop-entry-nl-01")
            first["metadata"]["id"] = "develop-entry-nl-02"
            first["spec"]["provider"]["resource_id"] = "fixture-entry-02"
            topology_fixture.put(desired_root, first)
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

    @unittest.skipIf(os.environ.get("SPIRITVPN_SKIP_LIVE_DESIRED") == "1", LIVE_DESIRED_SKIP_REASON)
    def test_cli_reports_success(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(["--root", str(REPO_ROOT), "validate", "--environment", "develop"])
        self.assertEqual(exit_code, 0)
        self.assertIn("develop: valid", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
