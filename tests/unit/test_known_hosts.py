from __future__ import annotations

from dataclasses import replace
import json
import re
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

from fleetctl.compiler import KnownHostsError, compile_known_hosts, render_files
from fleetctl.compiler.known_hosts import HOST_KEY_TYPES, host_pattern
from fleetctl.validation import validate_environment


REPO_ROOT = Path(__file__).resolve().parents[2]
VALID_DESIRED = REPO_ROOT / "tests" / "fixtures" / "valid" / "desired"

ENTRY_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIO/bJ1VpSI+1ILrrizOi8GYzc/gHkzYK7aSK870neakM"


class KnownHostsCompilerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.state = validate_environment(REPO_ROOT, "develop", desired_root=VALID_DESIRED)

    def entries(self, rendered: str) -> dict[str, str]:
        """Map every host pattern to the key declared for it."""

        mapping: dict[str, str] = {}
        for line in rendered.splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            patterns, key_type, key = line.split(" ")
            for pattern in patterns.split(","):
                mapping[pattern] = f"{key_type} {key}"
        return mapping

    def test_both_connection_phases_are_covered_by_one_key(self) -> None:
        entries = self.entries(compile_known_hosts(self.state))
        # Bootstrap dials the public address on 22; steady state dials the
        # management overlay on the declared port. One machine, one key.
        self.assertEqual(entries["192.0.2.10"], ENTRY_KEY)
        self.assertEqual(entries["[10.80.1.11]:232"], ENTRY_KEY)

    def test_bootstrap_host_key_uses_the_declared_non_default_port(self) -> None:
        instance = self.state.instances[0]
        state = replace(
            self.state,
            instances=(replace(instance, bootstrap_port=2222), *self.state.instances[1:]),
        )

        entries = self.entries(compile_known_hosts(state))

        self.assertEqual(entries["[192.0.2.10]:2222"], ENTRY_KEY)
        self.assertNotIn("192.0.2.10", entries)

    def test_a_non_default_port_is_bracketed_the_way_ssh_looks_it_up(self) -> None:
        self.assertEqual(host_pattern("10.80.1.11", 232), "[10.80.1.11]:232")
        self.assertEqual(host_pattern("2001:db8::1", 232), "[2001:db8::1]:232")
        # Port 22 is written bare; bracketing it would never match.
        self.assertEqual(host_pattern("192.0.2.10", 22), "192.0.2.10")
        self.assertEqual(host_pattern("2001:db8::1", 22), "2001:db8::1")

    # Схема — контракт, кортеж в компиляторе — то, что читает валидатор границы.
    # Разъехавшись, они дали бы ключ, который проходит валидацию desired state и
    # отвергается при сборке, то есть отказ уже после начала выкатки.
    def test_the_schema_and_the_compiler_allow_the_same_key_types(self) -> None:
        schema = json.loads(
            (REPO_ROOT / "contracts" / "desired-state" / "instance.schema.json").read_text(
                encoding="utf-8"
            )
        )
        pattern = schema["properties"]["spec"]["properties"]["ssh_host_key"]["pattern"]
        expression = re.compile(pattern)
        blob = "AAAAC3NzaC1lZDI1NTE5AAAAIONqHp8FDK9KZplnxmYwEP9ml2acc5yvypJ8fy1LEtEZ"
        for key_type in HOST_KEY_TYPES:
            with self.subTest(key_type=key_type):
                self.assertIsNotNone(expression.fullmatch(f"{key_type} {blob}"))
        for rejected in (
            f"ssh-dss {blob}",
            blob,
            f"ssh-ed25519 {blob} root@host",
            f"192.0.2.252 ssh-ed25519 {blob}",
        ):
            with self.subTest(rejected=rejected):
                self.assertIsNone(expression.fullmatch(rejected))

    # Поле объявлено необязательным в схеме, потому что impact plan валидирует
    # базовый коммит нынешними контрактами: сделай его обязательным — и любая
    # выкатка против базы, написанной до появления поля, упадёт на валидации.
    # Требование живёт в компиляторе, где видно, до кого выкатка дотягивается.
    def test_a_reachable_instance_without_a_key_refuses_to_compile(self) -> None:
        with tempfile.TemporaryDirectory(prefix="known-hosts-") as temporary:
            desired = Path(temporary) / "desired"
            shutil.copytree(VALID_DESIRED, desired)
            topology_fixture.edit(
                desired,
                "develop-exit-de-01",
                lambda document: document["spec"].pop("ssh_host_key"),
            )
            # Схему и семантику проходит — иначе базовый коммит было бы не прочитать.
            state = validate_environment(REPO_ROOT, "develop", desired_root=desired)
            with self.assertRaisesRegex(KnownHostsError, "develop-exit-de-01"):
                compile_known_hosts(state)

    def test_a_retired_instance_without_a_key_is_not_a_problem(self) -> None:
        # Машину вывели ещё до того, как поле появилось. Выкатка её не адресует,
        # и требовать ключ значило бы требовать его задним числом.
        with tempfile.TemporaryDirectory(prefix="known-hosts-") as temporary:
            desired = Path(temporary) / "desired"
            shutil.copytree(VALID_DESIRED, desired)
            document = topology_fixture.get(desired, "develop-exit-de-01")
            document["metadata"]["id"] = "develop-exit-de-02"
            document["spec"]["target_state"] = "retired"
            document["spec"]["public_address"] = "192.0.2.21"
            document["spec"]["provider"]["resource_id"] = "fixture-exit-02"
            del document["spec"]["ssh_host_key"]
            topology_fixture.put(desired, document)
            state = validate_environment(REPO_ROOT, "develop", desired_root=desired)

            entries = self.entries(compile_known_hosts(state))
            self.assertNotIn("192.0.2.21", entries)
            self.assertIn("192.0.2.20", entries)

    def test_the_file_is_marked_generated(self) -> None:
        self.assertTrue(compile_known_hosts(self.state).startswith("# GENERATED — DO NOT EDIT"))

    def test_rendering_is_deterministic_and_part_of_the_build(self) -> None:
        files = render_files(self.state)
        self.assertIn("known_hosts", files)
        self.assertEqual(files["known_hosts"], render_files(self.state)["known_hosts"])

    def test_a_retired_instance_is_no_longer_trusted(self) -> None:
        # Замена машины: рядом с работающим инстансом объявлен выведенный. Ни
        # один инвентарь его не адресует, и ключ обязан исчезнуть — иначе флот
        # продолжает доверять машине, которую отпустил.
        with tempfile.TemporaryDirectory(prefix="known-hosts-") as temporary:
            desired = Path(temporary) / "desired"
            shutil.copytree(VALID_DESIRED, desired)
            document = topology_fixture.get(desired, "develop-entry-nl-01")
            document["metadata"]["id"] = "develop-entry-nl-02"
            document["spec"]["target_state"] = "retired"
            document["spec"]["public_address"] = "192.0.2.11"
            document["spec"]["provider"]["resource_id"] = "fixture-entry-02"
            topology_fixture.put(desired, document)
            state = validate_environment(REPO_ROOT, "develop", desired_root=desired)

            entries = self.entries(compile_known_hosts(state))
            self.assertNotIn("192.0.2.11", entries)
            # Работающая машина того же узла по-прежнему на месте.
            self.assertIn("192.0.2.10", entries)


if __name__ == "__main__":
    unittest.main()
