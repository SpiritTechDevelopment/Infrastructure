from __future__ import annotations

from dataclasses import replace
import json
import re
import tempfile
import unittest
from pathlib import Path

import yaml

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

    def test_non_default_bootstrap_port_matches_pinned_known_hosts(self) -> None:
        state = validate_environment(REPO_ROOT, "develop", desired_root=VALID_DESIRED)
        instance = state.instances[0]
        state = replace(
            state,
            instances=(replace(instance, bootstrap_port=2222), *state.instances[1:]),
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "develop"
            write_rendered_files(output, render_files(state))

            self.assertEqual(validate_ansible_artifacts(output, "develop"), 2)
            self.assertIn("[192.0.2.10]:2222,", (output / "known_hosts").read_text())

    # known_hosts решает, к какой машине выкатка вообще согласится подключиться.
    # Расхождение с инвентарями обязано остановить прогон здесь, а не на проводе
    # посреди play, когда часть флота уже тронута.
    def test_missing_known_hosts_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = self.render(Path(temporary))
            (output / "known_hosts").unlink()
            with self.assertRaises(CompiledArtifactsError):
                validate_ansible_artifacts(output, "develop")

    def test_known_hosts_missing_one_endpoint_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = self.render(Path(temporary))
            path = output / "known_hosts"
            kept = [
                line
                for line in path.read_text(encoding="utf-8").splitlines(keepends=True)
                if "192.0.2.20" not in line
            ]
            path.write_text("".join(kept), encoding="utf-8")
            with self.assertRaisesRegex(CompiledArtifactsError, "missing="):
                validate_ansible_artifacts(output, "develop")

    def test_known_hosts_trusting_an_unreached_host_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = self.render(Path(temporary))
            path = output / "known_hosts"
            path.write_text(
                path.read_text(encoding="utf-8")
                + "198.51.100.7 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHwZ\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(CompiledArtifactsError, "unexpected="):
                validate_ansible_artifacts(output, "develop")

    def test_known_hosts_entry_that_is_not_a_host_line_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = self.render(Path(temporary))
            path = output / "known_hosts"
            path.write_text(
                path.read_text(encoding="utf-8") + "192.0.2.30 not-a-key\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(CompiledArtifactsError, "not a host entry"):
                validate_ansible_artifacts(output, "develop")

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

    def test_inventory_ssh_port_must_agree_with_the_node_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = self.render(Path(temporary))
            path = output / "ansible-inventory.json"
            inventory = json.loads(path.read_text(encoding="utf-8"))
            hosts = inventory["all"]["children"]["spiritvpn_fleet"]["children"]["entry"]["hosts"]
            hosts["develop-entry-nl-01"]["ansible_port"] = 22
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
        play = yaml.safe_load(playbook)[0]
        roles = [item["role"] for item in play["roles"]]
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
        self.assertIn("node_layout", roles)
        self.assertLess(roles.index("node_layout"), roles.index("compiled_runtime"))

    def test_readiness_rejects_a_stale_installed_node_plan(self) -> None:
        for relative in (
            "playbooks/bootstrap/readiness.yml",
            "playbooks/operations/readiness.yml",
        ):
            with self.subTest(playbook=relative):
                readiness = (REPO_ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("/etc/spiritvpn/node-plan.json", readiness)
                self.assertIn("| b64decode | from_json", readiness)
                self.assertIn("== spiritvpn_node_plan", readiness)
                self.assertIn("no_log: true", readiness)


class ContainerLogCeilingTests(unittest.TestCase):
    """Every container declares a log ceiling.

    У драйвера json-file нет предела по умолчанию, и растёт он молча: место на
    диске кончается раньше, чем кто-нибудь посмотрит в `docker logs`. Проверяется
    перечислением сервисов из самих шаблонов, а не списком в тесте, — иначе
    новый сервис приезжает на хост без предела и никто об этом не узнает.
    """

    TEMPLATES = (
        ("compiled_runtime", "compiled_runtime_log"),
        ("control_runtime", "control_log"),
        ("platform_observability", "platform_observability_log"),
    )

    def test_every_compose_service_declares_a_log_ceiling(self) -> None:
        for role, prefix in self.TEMPLATES:
            with self.subTest(role=role):
                path = REPO_ROOT / "roles" / role / "templates" / "compose.yml.j2"
                source = path.read_text(encoding="utf-8")
                # Jinja-выражения заменяются заглушкой: проверяется структура
                # отрендеренного compose, а не значения подстановок.
                rendered = re.sub(r"{{[^\n{}]+}}", "fixture", source)
                # Условные блоки снимаются целиком, а тело остаётся: сервис,
                # объявленный под `{% if %}`, доезжает до хоста ровно так же,
                # и предел на логи ему нужен ровно так же. Пропустить его
                # значило бы вернуть ту самую дыру, ради которой сервисы здесь
                # перечисляются из шаблона, а не списком в тесте.
                rendered = re.sub(r"(?m)^\s*{%[^\n]*%}\s*$\n?", "", rendered)
                document = yaml.safe_load(rendered)
                services = document["services"]
                self.assertTrue(services)
                for name, service in services.items():
                    logging = service.get("logging")
                    self.assertIsNotNone(logging, f"{role}/{name} declares no log ceiling")
                    self.assertEqual(logging["driver"], "json-file")
                    self.assertIn("max-size", logging["options"])
                    self.assertIn("max-file", logging["options"])

                defaults = (REPO_ROOT / "roles" / role / "defaults" / "main.yml").read_text(
                    encoding="utf-8"
                )
                values = yaml.safe_load(defaults)
                # Значения, а не только имена: пустой предел — это отсутствие предела.
                self.assertRegex(str(values[f"{prefix}_max_size"]), r"^[0-9]+[kmg]$")
                self.assertGreaterEqual(int(values[f"{prefix}_max_files"]), 1)

    def test_vault_is_deliberately_left_without_a_ceiling(self) -> None:
        """Touching Vault's compose file seals Vault.

        Изменение файла даёт `Recreated` на ближайшем `docker compose up -d`, а
        стансы `seal` в конфиге нет — Vault раскрывается вручную shamir-долями.
        Запечатанный Vault останавливает fleet-deploy и control-deploy. Тест
        держит это решение осознанным: если предел здесь однажды понадобится,
        добавлять его придётся вместе с церемонией раскрытия, а не мимоходом.
        """
        compose = (REPO_ROOT / "roles" / "platform_vault" / "templates" / "compose.yml.j2").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("logging:", compose)
        self.assertIn("запечатанным", compose)
        configuration = (
            REPO_ROOT / "roles" / "platform_vault" / "templates" / "vault.hcl.j2"
        ).read_text(encoding="utf-8")
        # Появится auto-unseal — перезапуск перестанет быть церемонией, и
        # причина исключения исчезнет вместе с этой проверкой.
        self.assertNotIn("seal ", configuration)

    def test_the_ceiling_is_not_set_through_the_docker_daemon(self) -> None:
        """The daemon route would need a Docker restart on a live hub.

        Демон читает log-opts при создании контейнера и не перечитывает их на
        SIGHUP, поэтому `/etc/docker/daemon.json` означал бы перезапуск Docker —
        то есть простой Vault, базы и бэкенда. Предел живёт в compose, и роль
        docker намеренно не трогает конфигурацию демона.
        """
        role = (REPO_ROOT / "roles" / "docker").rglob("*.yml")
        for path in role:
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("daemon.json", source)
            self.assertNotIn("log-opts", source)


if __name__ == "__main__":
    unittest.main()
