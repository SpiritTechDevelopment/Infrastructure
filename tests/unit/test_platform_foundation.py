from __future__ import annotations

import base64
import contextlib
import copy
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import yaml
from jinja2 import Environment

from fleetctl.validation import validate_environment


REPO_ROOT = Path(__file__).resolve().parents[2]
VALID_DESIRED = REPO_ROOT / "tests" / "fixtures" / "valid" / "desired"


class RecordingVault(BaseHTTPRequestHandler):
    """A stand-in Vault that records the calls the resolver actually makes."""

    calls: list[tuple[str, str, str | None]] = []
    fail_reads = False

    def log_message(self, *arguments: object) -> None:  # keep test output clean
        return

    def _respond(self, status: int, body: bytes = b"") -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        type(self).calls.append(("POST", self.path, self.headers.get("X-Vault-Token")))
        if self.path == "/v1/auth/approle/login":
            self._respond(200, json.dumps({"auth": {"client_token": "s.TEST"}}).encode())
        elif self.path == "/v1/auth/token/revoke-self":
            self._respond(204)  # Vault answers revocation with an empty body
        else:
            self._respond(404)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        type(self).calls.append(("GET", self.path, self.headers.get("X-Vault-Token")))
        if type(self).fail_reads:
            self._respond(500, b'{"errors":["unavailable"]}')
            return
        self._respond(
            200,
            json.dumps(
                {
                    "data": {
                        "data": {
                            "service_uuid": "0f9d3a2e-1111-2222-3333-444455556666",
                            "fullchain": "-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----",
                            "private_key": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----",
                        }
                    }
                }
            ).encode(),
        )


class PlatformFoundationTests(unittest.TestCase):
    def run_preflight(self, inventory: str, known_hosts: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory_path = root / "platform.yml"
            known_hosts_path = root / "known_hosts"
            inventory_path.write_text(inventory, encoding="utf-8")
            known_hosts_path.write_text(known_hosts, encoding="utf-8")
            return subprocess.run(
                [
                    "python3",
                    str(REPO_ROOT / "scripts" / "platform-bootstrap-check.py"),
                    "--inventory",
                    str(inventory_path),
                    "--known-hosts",
                    str(known_hosts_path),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

    def test_tracked_platform_bootstrap_is_sops_ciphertext_only(self) -> None:
        bootstrap = REPO_ROOT / "inventories" / "bootstrap"
        self.assertFalse((bootstrap / "platform.yml").exists())
        self.assertFalse((bootstrap / "known_hosts").exists())
        envelope = yaml.safe_load((bootstrap / "platform.sops.yml").read_text(encoding="utf-8"))
        self.assertEqual(envelope["apiVersion"], "spiritvpn.io/v1alpha1")
        self.assertEqual(envelope["kind"], "PlatformBootstrap")
        for field in ("inventory", "known_hosts", "vars"):
            self.assertTrue(envelope[field].startswith("ENC[AES256_GCM,"))
        ciphertext = (bootstrap / "platform.sops.yml").read_text(encoding="utf-8")
        for forbidden in ("ansible_host", "ssh-ed25519", "REPLACE_WITH", "github-develop"):
            self.assertNotIn(forbidden, ciphertext)

    def test_real_minimal_bootstrap_input_passes(self) -> None:
        fixture = REPO_ROOT / "tests" / "fixtures" / "platform-bootstrap"
        result = subprocess.run(
            [
                "python3",
                str(REPO_ROOT / "scripts" / "platform-bootstrap-check.py"),
                "--inventory",
                str(fixture / "platform.yml"),
                "--known-hosts",
                str(fixture / "known_hosts"),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("management-1 (1.1.1.1)", result.stdout)

    def test_private_wireguard_management_address_passes(self) -> None:
        result = self.run_preflight(
            """---
all:
  children:
    spiritvpn_platform_bootstrap:
      hosts:
        management-1:
          ansible_host: 10.70.0.1
          ansible_user: root
""",
            "10.70.0.1 ssh-ed25519 Zml4dHVyZS1wdWJsaWMta2V5\n",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_documentation_address_is_not_accepted_as_management(self) -> None:
        result = self.run_preflight(
            """---
all:
  children:
    spiritvpn_platform_bootstrap:
      hosts:
        management-1:
          ansible_host: 192.0.2.1
          ansible_user: root
""",
            "192.0.2.1 ssh-ed25519 Zml4dHVyZS1wdWJsaWMta2V5\n",
        )
        self.assertEqual(result.returncode, 2)

    def test_bootstrap_rejects_extra_inventory_variables(self) -> None:
        result = self.run_preflight(
            """---
all:
  children:
    spiritvpn_platform_bootstrap:
      hosts:
        management-1:
          ansible_host: 1.1.1.1
          ansible_user: root
          vault_token: forbidden
""",
            "1.1.1.1 ssh-ed25519 Zml4dHVyZS1wdWJsaWMta2V5\n",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("only ansible_host and ansible_user", result.stderr)

    def test_github_forced_command_has_environment_bound_operations(self) -> None:
        gate = REPO_ROOT / "roles" / "platform_executor" / "templates" / "spiritvpn-github-command.j2"
        subprocess.run(["bash", "-n", str(gate)], check=True)
        text = gate.read_text(encoding="utf-8")
        self.assertIn("original_command\" == platform-readiness", text)
        self.assertIn("fleet-deploy", text)
        self.assertIn("platform-deploy", text)
        self.assertIn("control-deploy", text)
        self.assertIn("cross-environment deployment", text)
        self.assertIn("bound_environment", text)
        self.assertNotIn("eval", text)
        self.assertNotIn("fleetctl deploy", text)
        self.assertNotIn("ansible-playbook", text)
        authorized_keys = (
            REPO_ROOT / "roles" / "platform_executor" / "templates" / "authorized_keys.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("spiritvpn-github-command {{ key.environment }}", authorized_keys)

    def test_remote_client_is_pinned_noninteractive_and_allowlisted(self) -> None:
        path = REPO_ROOT / "scripts" / "platform-remote.sh"
        subprocess.run(["bash", "-n", str(path)], check=True)
        text = path.read_text(encoding="utf-8")
        for required in ("StrictHostKeyChecking=yes", "BatchMode=yes", "IdentitiesOnly=yes"):
            self.assertIn(required, text)
        self.assertNotIn("ssh-keyscan", text)

    def test_operator_bootstrap_entrypoint_keeps_vault_ceremony_manual(self) -> None:
        path = REPO_ROOT / "scripts" / "platform-bootstrap.sh"
        subprocess.run(["bash", "-n", str(path)], check=True)
        text = path.read_text(encoding="utf-8")
        for required in (
            "git diff --quiet",
            "make check",
            "make lint",
            "fleet-platform-bootstrap-check",
            "fleet-platform-bootstrap",
            "sudo wg show spiritvpn-mgmt",
        ):
            self.assertIn(required, text)
        self.assertIn('[[ "$confirmation" == APPLY ]]', text)
        self.assertNotIn("vault operator init", text)
        self.assertNotIn("spiritvpn-vault-operator init", text)

    def test_vault_is_loopback_only_and_generates_tls_on_host(self) -> None:
        compose = (REPO_ROOT / "roles" / "platform_vault" / "templates" / "compose.yml.j2").read_text(
            encoding="utf-8"
        )
        tasks = (REPO_ROOT / "roles" / "platform_vault" / "tasks" / "main.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('"127.0.0.1:{{ platform_vault_api_port }}', compose)
        self.assertIn("- genpkey", tasks)
        self.assertNotIn("platform_vault_tls_private_key", tasks)
        self.assertNotIn("operator init", tasks)
        self.assertNotIn("operator unseal", tasks)
        defaults = (REPO_ROOT / "roles" / "platform_vault" / "defaults" / "main.yml").read_text(
            encoding="utf-8"
        )
        self.assertRegex(
            defaults,
            r"hashicorp/vault:1\.21\.4@sha256:[0-9a-f]{64}",
        )

    def test_platform_bootstrap_creates_wireguard_without_laptop_cidr(self) -> None:
        playbook = (REPO_ROOT / "playbooks" / "platform" / "bootstrap.yml").read_text(
            encoding="utf-8"
        )
        wireguard = (REPO_ROOT / "roles" / "platform_wireguard" / "tasks" / "main.yml").read_text(
            encoding="utf-8"
        )
        node_wireguard = (
            REPO_ROOT / "roles" / "bootstrap_wireguard" / "tasks" / "main.yml"
        ).read_text(encoding="utf-8")
        firewall = (REPO_ROOT / "roles" / "common" / "templates" / "nftables.conf.j2").read_text(
            encoding="utf-8"
        )
        self.assertIn("role: platform_wireguard", playbook)
        self.assertIn("Generate the management WireGuard private key locally once", wireguard)
        self.assertIn("creates: \"{{ platform_wireguard_private_key_path }}\"", wireguard)
        self.assertIn("Reconcile this node as a management-hub peer", node_wireguard)
        self.assertIn("delegate_to: localhost", node_wireguard)
        self.assertNotIn("platform_ssh_allowed_cidrs | join(' ')", playbook)
        self.assertIn("common_trusted_interfaces", firewall)
        self.assertIn("table inet spiritvpn_filter", firewall)

    def test_two_phase_platform_script_verifies_tunnel_before_hardening(self) -> None:
        script = (REPO_ROOT / "scripts" / "bootstrap-platform.py").read_text(
            encoding="utf-8"
        )
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        for required in (
            "playbooks/platform/wireguard-bootstrap.yml",
            "verifying pinned SSH through WireGuard",
            "playbooks/platform/bootstrap.yml",
            "refusing to overwrite unmanaged WireGuard config",
            "--verify-convergence",
        ):
            self.assertIn(required, script)
        self.assertLess(
            script.index("verifying pinned SSH through WireGuard"),
            script.index("phase 2/2: applying hardening"),
        )
        self.assertIn("scripts/bootstrap-platform.py --apply --verify-convergence", makefile)

        result = subprocess.run(
            [
                "python3",
                str(REPO_ROOT / "scripts" / "bootstrap-platform.py"),
                "--bundle",
                str(REPO_ROOT / "inventories" / "bootstrap" / "platform.sops.yml"),
                "--operator-wireguard-private-key",
                "/does/not/matter/without-apply",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("refusing mutation without --apply", result.stderr)

    def test_platform_executor_installs_a_compatible_python(self) -> None:
        defaults = (
            REPO_ROOT / "roles" / "platform_executor" / "defaults" / "main.yml"
        ).read_text(encoding="utf-8")
        tasks = (
            REPO_ROOT / "roles" / "platform_executor" / "tasks" / "main.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("platform_executor_python_minimum_minor: 11", defaults)
        self.assertIn("platform_executor_python_executable: /usr/bin/python3.11", defaults)
        self.assertIn("python3.11-venv", defaults)
        self.assertIn("platform_executor_python_executable", tasks)
        self.assertIn("Remove an incompatible executor Python environment", tasks)

    def test_github_workflow_cannot_mutate_or_receive_vault_credentials(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "platform-readiness.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("platform-remote.sh", workflow)
        self.assertIn("PLATFORM_SSH_PRIVATE_KEY", workflow)
        self.assertIn("secrets.PLATFORM_SSH_HOST", workflow)
        self.assertIn("secrets.PLATFORM_SSH_KNOWN_HOSTS", workflow)
        self.assertIn("runs-on: [self-hosted, linux, spiritvpn-deploy]", workflow)
        self.assertNotIn("vars.PLATFORM_SSH_HOST", workflow)
        self.assertNotIn("inventories/bootstrap/known_hosts", workflow)
        for forbidden in ("VAULT_TOKEN", "id-token: write", "fleet-deploy", "ansible-playbook", "ssh-keyscan"):
            self.assertNotIn(forbidden, workflow)

    def test_deploy_workflow_uses_reviewed_sha_bundle_and_no_vault_credential(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "fleet-deploy.yml").read_text(
            encoding="utf-8"
        )
        for required in (
            "workflow_dispatch",
            "git merge-base --is-ancestor",
            "git bundle create",
            "platform-remote.sh",
            # See test_control_deploy_is_digest_pinned_local_and_environment_bound:
            # the GitHub `environment:` key is unavailable on the free plan for
            # private repositories, so the binding is asserted at run time here
            # and by the forced command on the github-deploy key.
            '[[ "$REQUESTED_ENVIRONMENT" =~ ^(develop|prod)$ ]]',
            "secrets.PLATFORM_SSH_HOST",
            "secrets.PLATFORM_SSH_KNOWN_HOSTS",
            "runs-on: [self-hosted, linux, spiritvpn-deploy]",
        ):
            self.assertIn(required, workflow)
        self.assertNotIn("vars.PLATFORM_SSH_HOST", workflow)
        self.assertNotIn("inventories/bootstrap/known_hosts", workflow)
        for forbidden in ("VAULT_TOKEN", "id-token: write", "ssh-keyscan"):
            self.assertNotIn(forbidden, workflow)

    def test_deployment_ref_promotion_is_gated_and_separately_scoped(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "fleet-deploy.yml").read_text(
            encoding="utf-8"
        )
        deploy, separator, promote = workflow.partition("\n  promote:\n")
        self.assertTrue(separator, "fleet-deploy.yml has no promote job")
        # Право записи в репозиторий появляется ровно один раз, и не в той
        # задаче, которая держит SSH-ключ и разговаривает с хабом.
        self.assertEqual(workflow.count("contents: write"), 1)
        self.assertNotIn("contents: write", deploy)
        for forbidden in (
            "PLATFORM_SSH_PRIVATE_KEY",
            "platform-remote.sh",
            "self-hosted",
        ):
            self.assertNotIn(forbidden, promote)
        # Ref двигается только после отдельного подтверждения записью о
        # развёртывании и только через compare-and-swap с названной базой.
        for required in (
            "scripts/deployment-record.py",
            "if: inputs.mode == 'apply'",
            "needs.deploy.outputs.promote == 'true'",
            "--expected-baseline-git-sha",
            '--force-with-lease="$deployment_ref:$BASELINE_GIT_SHA"',
        ):
            self.assertIn(required, workflow)

    def test_platform_deploy_uses_exact_sha_and_local_protected_config(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "platform-deploy.yml").read_text(
            encoding="utf-8"
        )
        executor = (
            REPO_ROOT
            / "roles"
            / "platform_executor"
            / "templates"
            / "spiritvpn-platform-deploy.j2"
        ).read_text(encoding="utf-8")
        for required in (
            "git merge-base --is-ancestor",
            "git bundle create",
            "platform-remote.sh",
            # See test_control_deploy_is_digest_pinned_local_and_environment_bound:
            # the GitHub `environment:` key is unavailable on the free plan for
            # private repositories, so the binding is asserted at run time here
            # and by the forced command on the github-deploy key.
            '[[ "$REQUESTED_ENVIRONMENT" =~ ^(develop|prod)$ ]]',
            "runs-on: [self-hosted, linux, spiritvpn-deploy]",
        ):
            self.assertIn(required, workflow)
        for forbidden in ("VAULT_TOKEN", "SOPS_AGE_KEY", "ssh-keyscan"):
            self.assertNotIn(forbidden, workflow)
        for required in (
            'bundle verify "$bundle"',
            "refs/spiritvpn/platform-source",
            "playbooks/platform/steady.yml",
            "platform_runtime_vars_file",
        ):
            self.assertIn(required, executor)
        self.assertNotIn("eval", executor)

    def test_control_deploy_is_digest_pinned_local_and_environment_bound(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "control-deploy.yml").read_text(
            encoding="utf-8"
        )
        executor = (
            REPO_ROOT
            / "roles"
            / "platform_executor"
            / "templates"
            / "spiritvpn-control-deploy.j2"
        ).read_text(encoding="utf-8")
        compose = (
            REPO_ROOT / "roles" / "control_runtime" / "templates" / "compose.yml.j2"
        ).read_text(encoding="utf-8")
        tasks = (REPO_ROOT / "roles" / "control_runtime" / "tasks" / "main.yml").read_text(
            encoding="utf-8"
        )
        for required in (
            "git merge-base --is-ancestor",
            "refs/spiritvpn/control-source",
            "platform-remote.sh",
            # The environment binding is asserted at run time, not by a GitHub
            # `environment:` key. That key is unavailable on the free plan for
            # private repositories and made the job fail before its first step,
            # so it was removed; the binding it was standing in for lives here
            # and in the forced command attached to the github-deploy key.
            '[[ "$REQUESTED_ENVIRONMENT" =~ ^(develop|prod)$ ]]',
        ):
            self.assertIn(required, workflow)
        for forbidden in ("VAULT_TOKEN", "SOPS_AGE_KEY", "ssh-keyscan"):
            self.assertNotIn(forbidden, workflow)
        for required in (
            'bundle verify "$bundle"',
            "--scope control",
            "control-plan.json",
            "playbooks/control/deploy.yml",
            "refs/control-deployments/$environment",
        ):
            self.assertIn(required, executor)
        self.assertIn("control_plan.backend.image", compose)
        self.assertIn("control_plan.backend.migration_image", compose)
        self.assertIn("control_plan.postgres.image", compose)
        self.assertIn("Refuse an implicit PostgreSQL major-version upgrade", tasks)
        self.assertNotIn("--force-recreate", tasks)
        self.assertNotIn("compose\n      - down", tasks)

    def test_backend_runs_as_the_group_its_secrets_are_prepared_for(self) -> None:
        """The role chowns /secrets to a gid the container must actually hold.

        The image declares a numeric USER with no group, so the container's gid
        defaults to 0. Without an explicit gid here the process never joins the
        group, /secrets (0750 root:gid) is not traversable, and the backend dies
        on `permission denied` after a green migration — an unhealthy container
        with a healthy database behind it.
        """
        compose = (
            REPO_ROOT / "roles" / "control_runtime" / "templates" / "compose.yml.j2"
        ).read_text(encoding="utf-8")
        tasks = (REPO_ROOT / "roles" / "control_runtime" / "tasks" / "main.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'user: "{{ control_backend_uid }}:{{ control_backend_gid }}"',
            compose,
        )
        # The same two variables must be what the files are owned by, otherwise
        # the declaration above is decorative.
        self.assertIn("{{ control_backend_uid }}", tasks)
        self.assertIn("{{ control_backend_gid }}", tasks)

    def test_the_bot_reaches_the_public_internet_without_an_inbound_port(self) -> None:
        """Мини-апп публикуется исходящим туннелем, и это весь его вход.

        Опубликованный порт рядом с туннелем был бы вторым путём внутрь — мимо
        Cloudflare, без TLS и без единой проверки вызывающего. Проверяется по
        самому шаблону: `ports:` у сервисов бота не появляется, а туннель
        поднимается только после того, как мини-апп отвечает.
        """
        compose = yaml.safe_load(
            re.sub(
                r"(?m)^\s*{%[^\n]*%}\s*$\n?",
                "",
                re.sub(
                    r"{{[^\n{}]+}}",
                    "fixture",
                    (
                        REPO_ROOT / "roles" / "control_runtime" / "templates" / "compose.yml.j2"
                    ).read_text(encoding="utf-8"),
                ),
            )
        )
        services = compose["services"]
        for name in ("bot", "bot-api", "bot-tunnel"):
            with self.subTest(service=name):
                self.assertIn(name, services)
                self.assertNotIn("ports", services[name])
        self.assertEqual(
            services["bot-tunnel"]["depends_on"]["bot-api"]["condition"],
            "service_healthy",
        )
        # Бэкенд адресуется по имени из его сертификата: своей DNS-записи в
        # compose у хоста нет, а `backend:8443` предъявил бы имя, которого
        # сертификат не несёт.
        self.assertIn("extra_hosts", services["bot"])

    def test_bot_env_files_exist_before_any_compose_command(self) -> None:
        """Каждый вызов `docker compose` разбирает проект целиком.

        Регрессия, стоившая красной выкатки: подготовку бота перенесли в конец,
        и `compose.yml` стал ссылаться на ещё не написанный bot.env. Упала при
        этом проверка готовности **бэкенда** — команда `exec -T backend`, к боту
        отношения не имеющая. Отсюда правило: файлы бота пишутся до рендера
        compose, а раскатка бота остаётся в конце.
        """
        tasks = yaml.safe_load(
            (REPO_ROOT / "roles" / "control_runtime" / "tasks" / "main.yml").read_text(
                encoding="utf-8"
            )
        )
        names = [task["name"] for task in tasks]
        prepare = names.index("Prepare the bot's protected inputs")
        render = names.index("Render environment-isolated control compose definition")
        self.assertLess(prepare, render, "файлы бота обязаны быть написаны до рендера compose")

        first_compose = next(
            index
            for index, task in enumerate(tasks)
            if "docker" in str(task.get("ansible.builtin.command", {}).get("argv", ""))
            and "compose" in str(task.get("ansible.builtin.command", {}).get("argv", ""))
        )
        self.assertLess(
            prepare,
            first_compose,
            "ни одна команда compose не должна опережать запись env-файлов бота",
        )
        # Раскатка при этом остаётся последней: арендатор не роняет хозяина.
        self.assertEqual(names[-1], "Reconcile the bot beside the backend")

    def test_bot_migrations_ship_with_the_bot_image(self) -> None:
        """Схема и код приезжают одной парой.

        Отдельного образа миграций у бота нет — `alembic upgrade head`
        запускается из того же образа. Отдельный тег здесь означал бы схему из
        одной сборки под кодом из другой.
        """
        compose = (
            REPO_ROOT / "roles" / "control_runtime" / "templates" / "compose.yml.j2"
        ).read_text(encoding="utf-8")
        apply_tasks = (
            REPO_ROOT / "roles" / "control_runtime" / "tasks" / "bot-apply.yml"
        ).read_text(encoding="utf-8")
        self.assertEqual(compose.count("image: {{ control_plan.bot.image }}"), 3)
        self.assertIn("alembic", compose)
        # Миграции гоняются только на смене релиза, и признак релиза у бота
        # свой: выкатка бэкенда не должна их повторять.
        self.assertIn("_control_bot_release_changed", apply_tasks)

    def test_metrics_surfaces_never_leave_the_management_overlay(self) -> None:
        control_compose = (
            REPO_ROOT / "roles" / "control_runtime" / "templates" / "compose.yml.j2"
        ).read_text(encoding="utf-8")
        collector = REPO_ROOT / "roles" / "platform_observability"
        collector_tasks = (collector / "tasks" / "main.yml").read_text(encoding="utf-8")
        collector_compose = (
            collector / "templates" / "compose.yml.j2"
        ).read_text(encoding="utf-8")
        node_plan = (
            REPO_ROOT / "roles" / "compiled_node_plan" / "tasks" / "main.yml"
        ).read_text(encoding="utf-8")

        # The backend service port carries /metrics without TLS or caller
        # checks; publishing it on a wildcard would expose the whole fleet.
        self.assertIn(
            "{{ control_plan.network.management_address }}:"
            "{{ control_plan.network.backend_metrics_port }}:"
            "{{ control_plan.network.backend_metrics_port }}",
            control_compose,
        )
        self.assertNotIn("0.0.0.0", control_compose)

        # Scraping is outbound. The collector sits on a host that also holds
        # Vault, so it gets no inbound listener beyond loopback at all.
        self.assertIn(
            "--web.listen-address={{ platform_prometheus_bind_address }}",
            collector_compose,
        )
        self.assertIn("platform_prometheus_bind_address == '127.0.0.1'", collector_tasks)
        self.assertNotIn("--web.enable-admin-api", collector_compose)
        # The negated form, not `=false`: these are kingpin boolean flags that
        # take no value, so `=false` left a bare `false` positional behind and
        # Prometheus refused to start.
        self.assertIn("--no-web.enable-lifecycle", collector_compose)
        self.assertNotIn("--web.enable-lifecycle=", collector_compose)
        self.assertIn("promtool", collector_tasks)

        # Node-side listeners bind the overlay and the firewall repeats it.
        self.assertIn(
            "node_exporter_bind_address: "
            '"{{ spiritvpn_node_plan.instance.management_address }}"',
            node_plan,
        )
        self.assertIn("observability.ports.agent_metrics", node_plan)

    def test_one_collector_is_shared_but_each_environment_writes_only_its_own(self) -> None:
        collector = REPO_ROOT / "roles" / "platform_observability"
        skeleton = (collector / "templates" / "prometheus.yml.j2").read_text(encoding="utf-8")
        collector_tasks = (collector / "tasks" / "main.yml").read_text(encoding="utf-8")
        control_tasks = (
            REPO_ROOT / "roles" / "control_observability" / "tasks" / "main.yml"
        ).read_text(encoding="utf-8")
        steady = (REPO_ROOT / "playbooks" / "platform" / "steady.yml").read_text(
            encoding="utf-8"
        )
        executor = (
            REPO_ROOT
            / "roles"
            / "platform_executor"
            / "templates"
            / "spiritvpn-control-deploy.j2"
        ).read_text(encoding="utf-8")

        # The shared skeleton belongs to the platform contour, beside Vault.
        self.assertIn("role: platform_observability", steady)
        self.assertIn("file_sd_configs", skeleton)
        self.assertIn("platform_observability_environments", skeleton)

        # The environment-bound deployment writes fragments and nothing else:
        # no compose, no collector process, no shared configuration file.
        self.assertNotIn("docker", control_tasks)
        self.assertNotIn("compose", control_tasks)
        self.assertIn("fragment.json.j2", control_tasks)
        self.assertIn(
            "{{ collector_contract.targets_dir }}/{{ control_plan.environment }}/",
            control_tasks,
        )
        # Fragments written before the collector exists would be read by nobody.
        self.assertIn("Refuse to write fragments no collector will read", control_tasks)
        # A shared TSDB has one cadence; desired state must not claim another.
        self.assertIn(
            "collector_contract.scrape_interval_seconds | int",
            control_tasks,
        )
        # Only management-collected metrics are scraped: node-local expvar is
        # unreachable from here and external probes have no exporter yet.
        self.assertIn("selectattr('collection', 'equalto', 'management')", control_tasks)
        self.assertIn("monitoring_targets.environment == control_plan.environment", control_tasks)
        self.assertIn("monitoring-targets.json", executor)

        # A platform run must not erase what an environment wrote.
        self.assertIn("force: false", collector_tasks)

    def test_skeleton_reads_exactly_where_the_environments_write(self) -> None:
        """The two roles meet only at a path, and nothing else checks it.

        The skeleton names container paths, the fragments are written to host
        paths, and a bind mount joins them. Any of the three can be edited on
        its own, and the failure would be an empty graph rather than an error.
        """
        collector = REPO_ROOT / "roles" / "platform_observability"
        defaults = yaml.safe_load(
            (collector / "defaults" / "main.yml").read_text(encoding="utf-8")
        )
        jinja = Environment(  # noqa: S701 - rendering our own template for inspection
            keep_trailing_newline=True, trim_blocks=True, lstrip_blocks=False
        )
        jinja.filters["comment"] = lambda value: str(value)
        skeleton_source = (
            collector / "templates" / "prometheus.yml.j2"
        ).read_text(encoding="utf-8")
        skeleton = yaml.safe_load(
            jinja.from_string(skeleton_source).render(ansible_managed="", **defaults)
        )

        declared = {
            path
            for job in skeleton["scrape_configs"]
            for config in job.get("file_sd_configs", [])
            for path in config["files"]
        }
        host_directory = defaults["platform_observability_targets_dir"]
        container_directory = defaults["platform_prometheus_targets_container_dir"]

        # The compose definition is what makes the two path spaces the same
        # directory; without this mount the skeleton would read an empty path.
        compose = yaml.safe_load(
            jinja.from_string(
                (collector / "templates" / "compose.yml.j2").read_text(encoding="utf-8")
            ).render(**defaults)
        )
        self.assertIn(
            f"{host_directory}:{container_directory}:ro",
            compose["services"]["prometheus"]["volumes"],
        )

        expected = {
            f"{container_directory}/{environment}/{job['name']}.json"
            for environment in defaults["platform_observability_environments"]
            for job in defaults["platform_observability_jobs"]
        }
        self.assertEqual(declared, expected)
        # Every environment in the schema gets jobs, or its metrics silently
        # go nowhere.
        self.assertEqual(
            sorted(defaults["platform_observability_environments"]), ["develop", "prod"]
        )

    def test_root_executor_is_fail_closed_and_does_not_move_deployment_ref(self) -> None:
        path = REPO_ROOT / "roles" / "platform_executor" / "templates" / "spiritvpn-fleet-deploy.j2"
        text = path.read_text(encoding="utf-8")
        for required in (
            'bundle verify "$bundle"',
            "refs/spiritvpn/source",
            "vault-secret-resolver.py",
            "StrictHostKeyChecking=yes",
            "--state-dir",
            # Nodes keep their agent private keys, so the CA has to be reachable
            # from the executor or the bootstrap phase cannot complete.
            "--ca-state",
            '"ca/$environment/ca.key"',
        ):
            self.assertIn(required, text)
        self.assertNotIn("update-deployment-ref", text)
        self.assertNotIn("eval", text)

    def test_executor_trusts_only_compiled_host_keys(self) -> None:
        path = REPO_ROOT / "roles" / "platform_executor" / "templates" / "spiritvpn-fleet-deploy.j2"
        text = path.read_text(encoding="utf-8")
        # known_hosts перестал быть защищённым входом и стал артефактом сборки:
        # он компилируется из desired state тем же прогоном, который его читает.
        self.assertIn('for required in bootstrap.yml readiness.yml; do', text)
        self.assertNotIn("$config_dir/known_hosts", text)
        self.assertIn('build/$environment/known_hosts', text)
        self.assertIn("StrictHostKeyChecking=yes", text)
        # Обнаружение ключа на проводе не появляется ни здесь, ни где-либо ещё.
        self.assertNotIn("ssh-keyscan", text)

    def run_resolver_against_fake_vault(
        self, workspace: Path, *, fail_reads: bool
    ) -> subprocess.CompletedProcess[str]:
        RecordingVault.calls = []
        RecordingVault.fail_reads = fail_reads
        server = HTTPServer(("127.0.0.1", 0), RecordingVault)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            # The address is plain HTTP so the test needs no TLS, but the
            # resolver still builds an SSL context, so --vault-ca must parse.
            authority = workspace / "ca.crt"
            subprocess.run(
                [
                    "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(workspace / "ca.key"), "-out", str(authority),
                    "-days", "1", "-subj", "/CN=test",
                ],
                check=True,
                capture_output=True,
            )
            credentials = workspace / "approle"
            credentials.mkdir()
            (credentials / "role-id").write_text("role", encoding="utf-8")
            (credentials / "secret-id").write_text("secret", encoding="utf-8")
            return subprocess.run(
                [
                    "python3",
                    str(REPO_ROOT / "scripts" / "vault-secret-resolver.py"),
                    "--root", str(REPO_ROOT),
                    "--desired-root", str(VALID_DESIRED),
                    "--environment", "develop",
                    "--scope", "fleet",
                    "--credentials-dir", str(credentials),
                    "--compiled-secrets", str(workspace / "compiled.yml"),
                    "--vault-address", f"http://127.0.0.1:{server.server_address[1]}",
                    "--vault-ca", str(authority),
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
        finally:
            server.shutdown()
            server.server_close()

    def test_vault_token_is_revoked_on_success_and_on_failure(self) -> None:
        """A surviving AppRole token is a usable credential left on the executor.

        It outlives the process for its whole TTL otherwise, and the failure
        path is exactly where that matters most.
        """
        for fail_reads, expected_status in ((False, 0), (True, 2)):
            with self.subTest(fail_reads=fail_reads):
                with tempfile.TemporaryDirectory() as temporary:
                    workspace = Path(temporary)
                    result = self.run_resolver_against_fake_vault(
                        workspace, fail_reads=fail_reads
                    )
                    self.assertEqual(result.returncode, expected_status, result.stderr)
                    revocations = [
                        call
                        for call in RecordingVault.calls
                        if call[1] == "/v1/auth/token/revoke-self"
                    ]
                    self.assertEqual(len(revocations), 1, RecordingVault.calls)
                    self.assertEqual(revocations[0][2], "s.TEST")
                    self.assertEqual(revocations[0][0], "POST")
                    if not fail_reads:
                        compiled = workspace / "compiled.yml"
                        self.assertEqual(compiled.stat().st_mode & 0o777, 0o600)

    def test_vault_operator_is_manual_and_environment_scoped(self) -> None:
        operator = (
            REPO_ROOT / "roles" / "platform_vault" / "templates" / "spiritvpn-vault-operator.j2"
        ).read_text(encoding="utf-8")
        policy = (
            REPO_ROOT / "roles" / "platform_vault" / "templates" / "policy-fleet-deployer.hcl.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("operator init", operator)
        self.assertIn("operator unseal", operator)
        # The AppRole stays bound to callers on the management host itself, but
        # the bound set can no longer be the loopback literal alone: Vault runs
        # on a bridge network, so a login from the host arrives with its source
        # rewritten to the network gateway and was rejected outright. Both
        # bindings now come from the same computed pair, and the script refuses
        # to write the role at all if it cannot read the gateway.
        self.assertIn('local_cidrs="127.0.0.1/32,$gateway/32"', operator)
        self.assertIn('token_bound_cidrs="$local_cidrs"', operator)
        self.assertIn('secret_id_bound_cidrs="$local_cidrs"', operator)
        self.assertIn("cannot determine the Vault bridge gateway address", operator)
        self.assertIn("token_no_default_policy=true", operator)
        self.assertNotIn('-e "VAULT_TOKEN=$root_token"', operator)
        self.assertNotIn("| grep -q", operator)
        self.assertIn('auth_json="$(vault_as_root auth list -format=json)"', operator)
        self.assertIn('[[ "$auth_json" != *\'"approle/"\'* ]]', operator)
        self.assertIn('path "kv/data/{{ policy_environment }}/*"', policy)
        self.assertNotIn('capabilities = ["create"', policy)

    def test_secret_reference_listing_is_offline_and_environment_scoped(self) -> None:
        result = subprocess.run(
            [
                "python3",
                str(REPO_ROOT / "scripts" / "vault-secret-resolver.py"),
                "--root",
                str(REPO_ROOT),
                "--desired-root",
                str(REPO_ROOT / "tests" / "fixtures" / "valid" / "desired"),
                "--environment",
                "develop",
                "--list-references",
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        references = result.stdout.splitlines()
        self.assertTrue(references)
        self.assertEqual(references, sorted(references[:-1]) + [references[-1]])
        self.assertTrue(all(reference.startswith("secret://kv/develop/") for reference in references))
        self.assertEqual(
            references[-1],
            "secret://kv/develop/executor/ansible#private_key",
        )

    def test_control_secret_scope_excludes_fleet_and_executor_credentials(self) -> None:
        result = subprocess.run(
            [
                "python3",
                str(REPO_ROOT / "scripts" / "vault-secret-resolver.py"),
                "--root",
                str(REPO_ROOT),
                "--desired-root",
                str(REPO_ROOT / "tests" / "fixtures" / "valid" / "desired"),
                "--environment",
                "develop",
                "--scope",
                "control",
                "--list-references",
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        references = result.stdout.splitlines()
        # 11 бэкенда и 11 бота. Бот резолвится тем же scope: он разворачивается
        # тем же control-deploy, и его секреты обязаны доехать тем же проходом —
        # иначе роль упадёт на нерезолвленной ссылке уже на хосте.
        self.assertEqual(len(references), 22)
        self.assertTrue(
            all(reference.startswith("secret://kv/develop/control/") for reference in references)
        )
        self.assertTrue(any("/bot#telegram_bot_token" in item for item in references))
        self.assertFalse(any("executor/ansible" in reference for reference in references))

    def test_private_writer_refuses_symlink_and_sets_mode(self) -> None:
        script = REPO_ROOT / "scripts" / "vault-secret-resolver.py"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            link = root / "link"
            target.write_text("unchanged", encoding="utf-8")
            link.symlink_to(target)
            command = (
                "import importlib.util,pathlib;"
                f"s=importlib.util.spec_from_file_location('resolver',{str(script)!r});"
                "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
                f"p=pathlib.Path({str(link)!r});"
                "m.write_private(p,'secret')"
            )
            result = subprocess.run(
                ["python3", "-c", command], text=True, capture_output=True, check=False
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(target.read_text(encoding="utf-8"), "unchanged")

            output = root / "output"
            command = command.replace(str(link), str(output))
            result = subprocess.run(["python3", "-c", command], check=False)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)

    def test_executor_templates_have_valid_shell_after_jinja_substitution(self) -> None:
        paths = (
            REPO_ROOT / "roles" / "platform_executor" / "templates" / "spiritvpn-platform-readiness.j2",
            REPO_ROOT / "roles" / "platform_executor" / "templates" / "spiritvpn-fleet-deploy.j2",
            REPO_ROOT / "roles" / "platform_executor" / "templates" / "spiritvpn-platform-deploy.j2",
            REPO_ROOT / "roles" / "platform_executor" / "templates" / "spiritvpn-control-deploy.j2",
            REPO_ROOT / "roles" / "platform_vault" / "templates" / "spiritvpn-vault-operator.j2",
            REPO_ROOT / "roles" / "platform_wireguard" / "templates" / "spiritvpn-wireguard-reconcile.j2",
            REPO_ROOT / "roles" / "platform_wireguard" / "templates" / "spiritvpn-wireguard-peer.j2",
        )
        for path in paths:
            source = path.read_text(encoding="utf-8")
            Environment().parse(source)
            rendered = re.sub(r"{{[^\n{}]+}}", "fixture", source)
            self.assertNotIn("{{", rendered)
            subprocess.run(["bash", "-n"], input=rendered, text=True, check=True)


class AutomaticDesiredStateDeployTests(unittest.TestCase):
    """The push-triggered path may deploy develop, and only develop.

    Реактивная выкатка вызывает control-deploy и fleet-deploy напрямую в режиме
    apply. Гейта одобрения за ними нет: GitHub Environments недоступны на плане
    free, и вызываемые workflow это документируют. Значит единственное, что
    удерживает prod от выкатки по пушу, — отсечка в detect, и она проверяется
    здесь.
    """

    WORKFLOWS = REPO_ROOT / ".github" / "workflows"

    def load(self, name: str) -> dict:
        document = yaml.safe_load((self.WORKFLOWS / name).read_text(encoding="utf-8"))
        # PyYAML разбирает ключ `on:` как булево True — YAML 1.1.
        document["on"] = document.get("on") or document.get(True)
        return document

    def test_caller_passes_exactly_what_the_reusable_workflows_require(self) -> None:
        caller = self.load("desired-state-deploy.yml")
        for job, specification in caller["jobs"].items():
            target = specification.get("uses")
            if target is None:
                continue
            with self.subTest(job=job):
                callee = self.load(Path(target).name)
                declared = callee["on"]["workflow_call"]["inputs"]
                required = {name for name, value in declared.items() if value.get("required")}
                supplied = set(specification.get("with", {}))
                self.assertEqual(supplied, required)
                self.assertEqual(specification.get("secrets"), "inherit")

    def test_production_never_reaches_the_automatic_path(self) -> None:
        caller = (self.WORKFLOWS / "desired-state-deploy.yml").read_text(encoding="utf-8")
        self.assertIn("grep -vx prod", caller)
        for job in ("deploy-control", "deploy-fleet"):
            self.assertIn(job, caller)
        # Первая раскатка среды остаётся решением человека.
        self.assertIn("initial: false", caller)
        self.assertNotIn("initial: true", caller)

    def test_the_absent_approval_gate_is_not_claimed_to_exist(self) -> None:
        """The reason prod is excluded must stay true.

        Появится настоящий гейт — отсечку можно снимать; пока его нет, документ
        не должен утверждать обратное. Проверяется отсутствие job-level
        `environment:` в вызываемых workflow, то есть та самая причина.
        """
        for name in ("control-deploy.yml", "fleet-deploy.yml"):
            document = self.load(name)
            for job, specification in document["jobs"].items():
                with self.subTest(workflow=name, job=job):
                    self.assertNotIn("environment", specification)


PEER_PUBLIC_KEY = base64.b64encode(bytes(range(32))).decode("ascii")
HUB_PUBLIC_KEY = base64.b64encode(bytes(range(32, 64))).decode("ascii")
BUNDLE_VARIABLES = {
    "platform_operator_ssh_public_keys": ["ssh-ed25519 AAAA operator"],
    "platform_github_ssh_keys": [{"environment": "develop", "public_key": "ssh-ed25519 AAAA gh"}],
    "platform_ssh_allowed_cidrs": ["10.80.0.0/16"],
    "platform_fail2ban_ignore_cidrs": [],
    "platform_vault_node_id": "management-1",
    "platform_vault_tls_server_name": "vault.internal",
    "platform_wireguard_hub_addresses": {"develop": "10.80.0.1/16", "prod": "10.82.0.1/16"},
    "platform_wireguard_operator_peers": [
        {"id": "operator", "public_key": PEER_PUBLIC_KEY, "allowed_ips": ["10.80.0.9/32"]}
    ],
    # Именно эта тройка и завела задачу в тупик: значения есть в бандле, но на
    # захардененный хаб их до сих пор доносили руками.
    "platform_alertmanager_telegram_bot_token": "12345:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "platform_alertmanager_telegram_chat_id": "-100500",
    "platform_alertmanager_telegram_thread_id": "",
}


class _RecordedRun:
    """Stands in for every subprocess the bootstrap shells out to.

    Записывает командные строки и содержимое переданных extra-vars: на живом
    прогоне это ровно то, что доезжает до хаба, и ровно то, что нельзя проверить,
    сравнивая скрипт со строками.
    """

    def __init__(self, *, endpoint: str, marker: str, tunnel_up: bool) -> None:
        self.endpoint = endpoint
        self.marker = marker
        self.tunnel_up = tunnel_up
        self.commands: list[list[str]] = []
        self.extra_vars: dict[str, str] = {}

    def __call__(
        self,
        command: list[str],
        *,
        environment: dict[str, str] | None = None,
        input_data: bytes | None = None,
        capture: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        self.commands.append(list(command))
        for index, value in enumerate(command[:-1]):
            if value == "--extra-vars" and command[index + 1].startswith("@"):
                path = Path(command[index + 1][1:])
                self.extra_vars[path.name] = path.read_text(encoding="utf-8")
            elif value == "--extra-vars" and "platform_wireguard_metadata_output" in command[index + 1]:
                Path(json.loads(command[index + 1])["platform_wireguard_metadata_output"]).write_text(
                    json.dumps(
                        {
                            "interface": "wg0",
                            "listen_port": 51820,
                            "hub_addresses": BUNDLE_VARIABLES["platform_wireguard_hub_addresses"],
                            "public_key": HUB_PUBLIC_KEY,
                        }
                    ),
                    encoding="utf-8",
                )
        stdout = b""
        returncode = 0
        if "head" in command:
            stdout = f"{self.marker}\n".encode()
        elif "cmp" in command:
            returncode = 1  # the rendered client config always differs from what is installed
        elif "cat" in command:
            stdout = (
                f"{self.marker}\n[Interface]\nPrivateKey = OPERATOR-PRIVATE-KEY\n"
                f"Address = 10.80.0.9/32\n\n[Peer]\nPublicKey = {HUB_PUBLIC_KEY}\n"
                f"AllowedIPs = 10.80.0.0/16\nEndpoint = {self.endpoint}\n"
                "PersistentKeepalive = 25\n"
            ).encode()
        elif "link" in command and "show" in command:
            returncode = 0 if self.tunnel_up else 1
        return subprocess.CompletedProcess(command, returncode, stdout, b"")


class HardenedHubBundleDeliveryTests(unittest.TestCase):
    """The supported way to deliver reviewed bundle values to a hardened hub.

    Публичный SSH на захардененном хабе закрыт по замыслу, поэтому первая фаза
    бутстрапа до него не доходит. Здесь закреплено, что режим --reuse-tunnel
    доносит бандл по уже поднятому туннелю и что он не трогает публичный путь.
    """

    module: object

    @classmethod
    def setUpClass(cls) -> None:
        path = REPO_ROOT / "scripts" / "bootstrap-platform.py"
        spec = importlib.util.spec_from_file_location("spiritvpn_bootstrap_platform", path)
        assert spec is not None and spec.loader is not None
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def load_module(self) -> object:
        return type(self).module

    def run_bootstrap(
        self,
        *,
        reuse_tunnel: bool,
        endpoint: str = "1.1.1.1:51820",
        marker: str | None = None,
        tunnel_up: bool = True,
    ) -> tuple[object, _RecordedRun]:
        module = self.load_module()
        recorder = _RecordedRun(
            endpoint=endpoint,
            marker=module.CLIENT_MARKER if marker is None else marker,
            tunnel_up=tunnel_up,
        )
        inventory = yaml.safe_load(
            (REPO_ROOT / "tests" / "fixtures" / "platform-bootstrap" / "platform.yml").read_text(
                encoding="utf-8"
            )
        )
        known_hosts = (
            REPO_ROOT / "tests" / "fixtures" / "platform-bootstrap" / "known_hosts"
        ).read_text(encoding="utf-8")

        class _Bundle:
            @staticmethod
            def decrypt_bundle(path: Path) -> tuple[dict, str, dict]:
                return copy.deepcopy(inventory), known_hosts, copy.deepcopy(BUNDLE_VARIABLES)

        module._load_platform_sops = lambda: _Bundle
        module._require_command = lambda command: command
        module._validate_private_key = lambda path: PEER_PUBLIC_KEY
        module._run = recorder
        with tempfile.TemporaryDirectory() as temporary:
            key = Path(temporary) / "operator.key"
            key.write_text("unused\n", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):  # keep test output clean
                module.execute(
                    bundle=Path("/does/not/matter"),
                    operator_private_key=key,
                    client_interface="spiritvpn-mgmt",
                    verify_convergence=False,
                    reuse_tunnel=reuse_tunnel,
                )
        return module, recorder

    def applied_command(self, recorder: _RecordedRun) -> list[str]:
        applied = [
            command
            for command in recorder.commands
            if "playbooks/platform/bootstrap.yml" in command and "--syntax-check" not in command
        ]
        self.assertEqual(len(applied), 1)
        return applied[0]

    def test_reused_tunnel_delivers_the_bundle_without_touching_public_ssh(self) -> None:
        _, recorder = self.run_bootstrap(reuse_tunnel=True)

        for command in recorder.commands:
            self.assertNotIn("playbooks/platform/wireguard-bootstrap.yml", command)
            if command[0] == "ansible":
                self.assertNotIn("--inventory", command)
                joined = " ".join(command)
                self.assertNotIn("public-inventory.yml", joined)
                self.assertIn("internal-inventory.yml", joined)
        # Локальный туннель переиспользуется, а не переписывается: чужой конфиг
        # не заменяется, а wg-quick не перезапускается под работающим прогоном.
        self.assertFalse(any("wg-quick" in command for command in recorder.commands))

        applied = self.applied_command(recorder)
        self.assertIn("internal-inventory.yml", " ".join(applied))
        self.assertIn(json.dumps({"deploy_mode": "hardened"}), applied)

        delivered = yaml.safe_load(recorder.extra_vars["runtime-vars.yml"])
        self.assertEqual(
            delivered["platform_alertmanager_telegram_bot_token"],
            BUNDLE_VARIABLES["platform_alertmanager_telegram_bot_token"],
        )
        self.assertEqual(delivered["platform_wireguard_public_endpoint"], "1.1.1.1:51820")

    def test_reused_tunnel_refuses_a_tunnel_it_did_not_create_or_cannot_trust(self) -> None:
        module = self.load_module()
        for description, keywords in (
            ({"marker": "# hand-written"}, "unmanaged"),
            ({"endpoint": "9.9.9.9:51820"}, "does not lead to the hub"),
            ({"tunnel_up": False}, "is not up"),
        ):
            with self.subTest(**description):
                with self.assertRaises(module.BootstrapError) as raised:
                    self.run_bootstrap(reuse_tunnel=True, **description)
                self.assertIn(keywords, str(raised.exception))

    def test_clean_host_bootstrap_still_creates_the_tunnel_it_needs(self) -> None:
        _, recorder = self.run_bootstrap(reuse_tunnel=False)

        self.assertTrue(
            any("playbooks/platform/wireguard-bootstrap.yml" in c for c in recorder.commands)
        )
        self.assertTrue(any("wg-quick" in command for command in recorder.commands))
        applied = self.applied_command(recorder)
        # Первый прогон обязан оставаться в режиме bootstrap: он меняет sshd и
        # firewall под собственным соединением и держит для этого порт 22.
        self.assertNotIn(json.dumps({"deploy_mode": "hardened"}), applied)
        delivered = yaml.safe_load(recorder.extra_vars["runtime-vars.yml"])
        self.assertEqual(delivered["platform_wireguard_public_endpoint"], "1.1.1.1:51820")

    def test_every_bundle_variable_is_persisted_by_the_executor(self) -> None:
        """A bundle key the hub never writes down is delivered nowhere."""
        path = REPO_ROOT / "scripts" / "platform-sops.py"
        spec = importlib.util.spec_from_file_location("spiritvpn_platform_sops", path)
        assert spec is not None and spec.loader is not None
        sops = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sops)

        tasks = (REPO_ROOT / "roles" / "platform_executor" / "tasks" / "main.yml").read_text(
            encoding="utf-8"
        )
        start = tasks.index("Persist the bounded management runtime configuration")
        persisted = set(re.findall(r"'(platform_[a-z0-9_]+)':", tasks[start : tasks.index("dest:", start)]))
        self.assertTrue(persisted)
        self.assertLessEqual(sops.EXPECTED_VARIABLE_KEYS, persisted)


if __name__ == "__main__":
    unittest.main()
