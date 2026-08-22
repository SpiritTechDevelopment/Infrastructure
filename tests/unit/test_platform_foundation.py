from __future__ import annotations

import base64
import contextlib
import copy
import importlib.util
import io
import ipaddress
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

from fleetctl.compiler import compile_node_plans
from fleetctl.validation import validate_environment


REPO_ROOT = Path(__file__).resolve().parents[2]
VALID_DESIRED = REPO_ROOT / "tests" / "fixtures" / "valid" / "desired"
LIVE_DESIRED_SKIP_REASON = "encrypted repository desired state requires a trusted SOPS identity"


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

    def test_topology_sops_keeps_recovery_and_executor_recipients(self) -> None:
        config = yaml.safe_load((REPO_ROOT / ".sops.yaml").read_text(encoding="utf-8"))
        topology_rule = next(
            rule
            for rule in config["creation_rules"]
            if "topology" in rule["path_regex"]
        )
        recipients = [value.strip() for value in topology_rule["age"].split(",")]
        self.assertEqual(len(recipients), 3)
        self.assertEqual(len(set(recipients)), 3)
        for recipient in recipients:
            self.assertRegex(recipient, r"^age1[0-9a-z]+$")

        platform_rule = next(
            rule
            for rule in config["creation_rules"]
            if "inventories/bootstrap/platform" in rule["path_regex"]
        )
        platform_recipients = [value.strip() for value in platform_rule["age"].split(",")]
        self.assertEqual(len(platform_recipients), 2)
        platform_envelope = yaml.safe_load(
            (REPO_ROOT / "inventories" / "bootstrap" / "platform.sops.yml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            {item["recipient"] for item in platform_envelope["sops"]["age"]},
            set(platform_recipients),
        )

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
        self.assertEqual(yaml.safe_load(defaults)["platform_vault_image"], "")
        projection = (REPO_ROOT / "scripts" / "platform-component-vars.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"vault": "platform_vault_image"', projection)
        self.assertIn("requires an immutable digest", projection)

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

    def test_bridged_containers_on_the_hub_may_dial_out(self) -> None:
        """Политика forward — drop, и это ловушка ровно для control-стека.

        На нодах и во всём платформенном контуре контейнеры стоят в сети хоста
        и цепочку forward не проходят вовсе. Мостовая сеть только у control, и
        первый же его контейнер, которому понадобился интернет (туннель мини-
        аппа), упёрся в таймаут на любом порту при живом выходе с самого хоста.

        Проверяется вместе с тем, что разрешение осталось односторонним:
        `oifname` для мостов не добавляется, иначе внутрь контейнеров начало бы
        проходить всё, а не только DNAT опубликованных портов.
        """
        firewall = (REPO_ROOT / "roles" / "common" / "templates" / "nftables.conf.j2").read_text(
            encoding="utf-8"
        )
        self.assertIn("common_container_egress_interfaces", firewall)
        self.assertIn('iifname "{{ interface }}" accept', firewall)
        # Умолчание пустое: ноде это не нужно и открывать ей нечего.
        defaults = yaml.safe_load(
            (REPO_ROOT / "roles" / "common" / "defaults" / "main.yml").read_text(encoding="utf-8")
        )
        self.assertEqual(defaults["common_container_egress_interfaces"], [])

        # Оба платформенных playbook'а обязаны объявлять одно и то же: хаб, у
        # которого выход из контейнеров зависит от того, каким путём его
        # раскатывали, — это тот же таймаут, только позже.
        for name in ("steady", "bootstrap"):
            with self.subTest(playbook=name):
                playbook = yaml.safe_load(
                    (REPO_ROOT / "playbooks" / "platform" / f"{name}.yml").read_text(
                        encoding="utf-8"
                    )
                )
                declared = playbook[0]["vars"]["common_container_egress_interfaces"]
                self.assertEqual(declared, ["docker0", "br-*"])

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

    def test_platform_executor_owns_its_sops_age_identity(self) -> None:
        defaults = (
            REPO_ROOT / "roles" / "platform_executor" / "defaults" / "main.yml"
        ).read_text(encoding="utf-8")
        tasks = (
            REPO_ROOT / "roles" / "platform_executor" / "tasks" / "main.yml"
        ).read_text(encoding="utf-8")
        fleet_executor = (
            REPO_ROOT
            / "roles"
            / "platform_executor"
            / "templates"
            / "spiritvpn-fleet-deploy.j2"
        ).read_text(encoding="utf-8")
        platform_executor = (
            REPO_ROOT
            / "roles"
            / "platform_executor"
            / "templates"
            / "spiritvpn-platform-deploy.j2"
        ).read_text(encoding="utf-8")
        readiness = (
            REPO_ROOT
            / "roles"
            / "platform_executor"
            / "templates"
            / "spiritvpn-platform-readiness.j2"
        ).read_text(encoding="utf-8")

        self.assertIn("platform_executor_sops_version: \"3.13.3\"", defaults)
        self.assertIn("sha256:e5bec3346a873ae91d871550f3e698c1", defaults)
        self.assertIn("age-keygen", tasks)
        self.assertIn("creates: \"{{ platform_executor_sops_identity_file }}\"", tasks)
        self.assertIn('mode: "0600"', tasks)
        self.assertNotIn("src: \"{{ platform_executor_sops_identity_file }}\"", tasks)
        self.assertIn("SOPS_AGE_KEY_FILE", fleet_executor)
        self.assertIn("SOPS_AGE_KEY_FILE", platform_executor)
        self.assertIn("materialize-runtime", platform_executor)
        self.assertIn("inventories/bootstrap/platform.sops.yml", platform_executor)
        self.assertIn("--require-applied-runtime", platform_executor)
        self.assertIn('--extra-vars "@$platform_vars"', platform_executor)
        self.assertNotIn('--extra-vars "@{{ platform_runtime_vars_file }}"', platform_executor)
        self.assertIn("age-keygen -y", readiness)
        self.assertIn("cmp -s", readiness)

    def test_runner_sops_identity_is_local_and_not_a_github_secret(self) -> None:
        bootstrap = (REPO_ROOT / "scripts" / "bootstrap-runner-sops.sh").read_text(
            encoding="utf-8"
        )
        workflow = (
            REPO_ROOT / ".github" / "workflows" / "runner-sops-bootstrap.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("age-identity.txt", bootstrap)
        self.assertIn("age-recipient.txt", bootstrap)
        self.assertIn("chmod 0600", bootstrap)
        self.assertIn("SOPS checksum does not match", bootstrap)
        self.assertIn("age checksum does not match", bootstrap)
        self.assertIn("runs-on: [self-hosted, linux, spiritvpn-deploy]", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertNotIn("SOPS_AGE_KEY", workflow)

    def test_control_executor_can_decrypt_encrypted_topology(self) -> None:
        executor = (
            REPO_ROOT
            / "roles"
            / "platform_executor"
            / "templates"
            / "spiritvpn-control-deploy.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("SOPS_AGE_KEY_FILE", executor)

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
            "scripts/platform-component-vars.py",
            "desired/common/components.yml",
            "SOPS_AGE_KEY_FILE",
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
            "control-contract-check.py",
            "--require-applied",
            "playbooks/control/deploy.yml",
            "refs/control-deployments/$environment",
        ):
            self.assertIn(required, executor)
        self.assertNotIn('--extra-vars "@$config_dir/control.yml"', executor)
        self.assertIn("control_plan.backend.image", compose)
        self.assertIn("control_plan.backend.migration_image", compose)
        self.assertIn("control_plan.postgres.image", compose)
        self.assertIn("Refuse an implicit PostgreSQL major-version upgrade", tasks)
        self.assertNotIn("--force-recreate", tasks)
        self.assertNotIn("compose\n      - down", tasks)

    def test_control_backup_contract_is_git_owned_and_local_drift_fails_closed(self) -> None:
        path = REPO_ROOT / "scripts" / "control-contract-check.py"
        spec = importlib.util.spec_from_file_location("spiritvpn_control_contract", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = root / "control-plan.json"
            plan.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "postgres": {
                            "external_backup_command_argv": [
                                "/usr/local/sbin/reviewed-backup",
                                "develop",
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )
            applied = root / "control.yml"
            applied.write_text(
                yaml.safe_dump(
                    {
                        "control_external_backup_command_argv": [
                            "/usr/local/sbin/reviewed-backup",
                            "develop",
                        ]
                    }
                ),
                encoding="utf-8",
            )
            module.require_applied_contract(plan, applied)

            applied.write_text(
                yaml.safe_dump({"control_external_backup_command_argv": []}),
                encoding="utf-8",
            )
            with self.assertRaises(module.ControlContractError) as raised:
                module.require_applied_contract(plan, applied)
            self.assertIn("explicitly applied approval", str(raised.exception))

        defaults = (
            REPO_ROOT / "roles" / "control_runtime" / "defaults" / "main.yml"
        ).read_text(encoding="utf-8")
        tasks = (
            REPO_ROOT / "roles" / "control_runtime" / "tasks" / "main.yml"
        ).read_text(encoding="utf-8")
        self.assertNotIn("control_external_backup_command_argv", defaults)
        self.assertIn("control_plan.postgres.external_backup_command_argv", tasks)

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

    def test_every_scalar_bot_secret_is_trimmed_at_the_point_of_use(self) -> None:
        """Обрезка обязана быть одинаковой везде, иначе пароль расходится с DSN.

        Церемония записи читает значение до EOF, поэтому вставка, законченная
        Enter, приносит завершающий перенос — это артефакт ввода, а не угроза.
        Опасен перенос внутри значения: env-файл построчный.

        Но главное здесь — согласованность. Обрезать DSN и не обрезать пароль
        значит записать в PostgreSQL пароль с переносом, а подключаться без
        него: отказ аутентификации при двух значениях, неразличимых в логах.
        """
        pem_refs = {
            "grpc_client_certificate_ref",
            "grpc_client_private_key_ref",
            "grpc_server_ca_ref",
        }
        pattern = re.compile(
            r"control_secret_values\[control_plan\.bot\.secret_refs\.(\w+)\]\s*(\|\s*trim)?"
        )
        untrimmed: list[str] = []
        for name in ("bot-prepare.yml", "bot-apply.yml"):
            source = (
                REPO_ROOT / "roles" / "control_runtime" / "tasks" / name
            ).read_text(encoding="utf-8")
            for reference, trimmed in pattern.findall(source):
                # PEM многострочен по своей природе и пишется файлом, не в env.
                if reference in pem_refs or trimmed:
                    continue
                untrimmed.append(f"{name}: {reference}")
        self.assertEqual(untrimmed, [])

        prepare = (
            REPO_ROOT / "roles" / "control_runtime" / "tasks" / "bot-prepare.yml"
        ).read_text(encoding="utf-8")
        # \S, а не [^ ]: последний пропускает перенос внутри значения, потому что
        # в Python `$` совпадает и перед завершающим переводом строки.
        self.assertIn(r"^postgresql\+asyncpg://\S+$", prepare)
        self.assertNotIn(r"[^ ]+$", prepare)

    def test_bot_pem_secrets_are_checked_for_being_pem(self) -> None:
        """Непустое — ещё не сертификат.

        Церемония записи читает до EOF, поэтому не сработавшая вставка кладёт
        один перевод строки: проверку на непустоту он проходит, а падает потом
        в openssl как «Unable to load certificate» — сообщением про файл, а не
        про то, как его наполняли.
        """
        prepare = yaml.safe_load(
            (
                REPO_ROOT / "roles" / "control_runtime" / "tasks" / "bot-prepare.yml"
            ).read_text(encoding="utf-8")
        )
        names = [task["name"] for task in prepare]
        guard = names.index("Require bot PEM secrets to actually be PEM")
        # Раньше записи файлов: смысл в том, чтобы не дойти до openssl.
        self.assertLess(guard, names.index("Install protected bot secret files"))
        checked = str(prepare[guard]["loop"])
        for reference in (
            "grpc_client_certificate_ref",
            "grpc_client_private_key_ref",
            "grpc_server_ca_ref",
        ):
            self.assertIn(reference, checked)

    def test_every_required_node_component_is_actually_deployed(self) -> None:
        """Обязательный компонент, который никто не разворачивает, — ложь.

        `alloy` был объявлен обязательным для ноды и закреплён digest'ом, но не
        упоминался ни в одном compose и ни в одном компиляторе. Валидация
        требовала его наличия в desired state, на нодах его не было, и заметить
        это было нечем. Тест перечисляет требуемое из кода валидации, а не
        списком здесь.
        """
        semantic = (REPO_ROOT / "fleetctl" / "validation" / "semantic.py").read_text(
            encoding="utf-8"
        )
        declared = re.search(r"required_node_components = \{([^}]*)\}", semantic)
        self.assertIsNotNone(declared)
        required = set(re.findall(r'"([a-z_]+)"', declared.group(1)))
        self.assertIn("alloy", required)

        compose = (
            REPO_ROOT / "roles" / "compiled_runtime" / "templates" / "compose.yml.j2"
        ).read_text(encoding="utf-8")
        plan = (REPO_ROOT / "roles" / "compiled_node_plan" / "tasks" / "main.yml").read_text(
            encoding="utf-8"
        )
        for component in sorted(required):
            with self.subTest(component=component):
                # Образ обязан приезжать из плана, а не из умолчания роли.
                self.assertIn(f"components.{component}.digest", plan)
                self.assertIn(f"{{{{ {component}_image }}}}", compose)

    def test_logs_never_carry_the_xray_access_log(self) -> None:
        """Запрет на вывоз access-лога держится в трёх местах сразу.

        В нём адреса клиентов и адреса назначения — ровно то, ради неведения
        чего сервис существует. Валидация запрещает включать экспорт, компилятор
        фиксирует решение в плане ноды, а шипер физически не видит каталог.
        """
        semantic = (REPO_ROOT / "fleetctl" / "validation" / "semantic.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("ACCESS_LOG_EXPORT", semantic)

        plans = (REPO_ROOT / "fleetctl" / "compiler" / "node_plans.py").read_text(encoding="utf-8")
        self.assertIn('"xray_access_log_exported": False', plans)

        compose = yaml.safe_load(
            re.sub(
                r"{{[^\n{}]+}}",
                "fixture",
                (
                    REPO_ROOT / "roles" / "compiled_runtime" / "templates" / "compose.yml.j2"
                ).read_text(encoding="utf-8"),
            )
        )
        alloy_volumes = compose["services"]["alloy"]["volumes"]
        self.assertTrue(alloy_volumes)
        for volume in alloy_volumes:
            self.assertNotIn("/var/log/xray", volume)
        # Xray свой каталог монтирует — значит проверка выше не проходит просто
        # потому, что каталога нет ни у кого.
        self.assertTrue(
            any("/var/log/xray" in volume for volume in compose["services"]["xray"]["volumes"])
        )

    @unittest.skipIf(os.environ.get("SPIRITVPN_SKIP_LIVE_DESIRED") == "1", LIVE_DESIRED_SKIP_REASON)
    def test_logs_leave_the_node_only_over_the_overlay(self) -> None:
        """Тот же инвариант, что у скрейп-целей, но в другую сторону.

        Скрейп из-за пределов оверлея просто не соберётся; отправка логов на
        адрес вне оверлея выпустила бы их наружу. Адрес поэтому выводится из
        сети управления, а не объявляется в desired state.
        """
        state = validate_environment(REPO_ROOT, "develop")
        plans = compile_node_plans(state)
        self.assertTrue(plans)
        network = ipaddress.ip_network(state.environment.management_network, strict=True)
        for instance_id, plan in plans.items():
            with self.subTest(instance=instance_id):
                endpoint = plan["logs"]["endpoint"]
                host = endpoint.split("//", 1)[1].split(":", 1)[0]
                self.assertIn(ipaddress.ip_address(host), network)

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

        # Grafana — исключение среди слушателей хаба: у неё есть человеческий
        # интерфейс, и операторы достают её через управляющий оверлей. Wildcard
        # ей по-прежнему запрещён, иначе она оказалась бы и на публичном
        # интерфейсе.
        self.assertIn(
            "platform_grafana_bind_address not in ['0.0.0.0', '*', '']",
            collector_tasks,
        )
        # Условие, ради которого это вообще безопасно. На хабе wg0 доверен
        # целиком: правило `iifname` срабатывает раньше любого ограничивающего,
        # поэтому порт на оверлейном адресе достижим для каждой ноды флота, и
        # firewall тут не поможет. Единственное, что отделяет дашборды с
        # составом флота и объёмами трафика от узла, вышедшего в оверлей, —
        # выключенный анонимный доступ.
        self.assertIn("not (platform_grafana_anonymous_enabled | bool)", collector_tasks)
        self.assertIn(
            'GF_AUTH_ANONYMOUS_ENABLED: "{{ platform_grafana_anonymous_enabled',
            collector_compose,
        )
        for playbook in ("steady", "bootstrap"):
            with self.subTest(playbook=playbook):
                text = (
                    REPO_ROOT / "playbooks" / "platform" / f"{playbook}.yml"
                ).read_text(encoding="utf-8")
                self.assertIn("platform_grafana_anonymous_enabled: false", text)
                self.assertIn("platform_grafana_bind_address", text)

                # Оверлей не доверен целиком. Пока `iifname wg0 accept` стоял в
                # input, ни один порт хаба нельзя было ограничить: правило по
                # интерфейсу срабатывало раньше любого правила по адресу, и до
                # всего, что слушает оверлейный адрес, дотягивалась каждая нода
                # флота. Возврат этой строки молча отменил бы разграничение
                # Vault и Grafana, ничего при этом не сломав видимо.
                self.assertIn("common_trusted_interfaces: []", text)
                # Транзит оператор → хаб → нода живёт отдельно от доверия ко
                # входу. Без него сужение отрезало бы операторов от нод.
                self.assertIn(
                    'common_forward_interfaces: ["{{ platform_wireguard_interface }}"]',
                    text,
                )
                # Источники выводятся из ростера, а не перечисляются: список,
                # который ведут руками, расходится с ростером в тот момент,
                # когда оператора добавили, а сюда дописать забыли.
                self.assertIn("platform_wireguard_operator_peers", text)
                self.assertIn("platform_operator_overlay_cidrs", text)

        # Транзитное правило обязано зависеть от отдельной переменной, иначе
        # сузить вход нельзя, не сломав пересылку.
        firewall = (
            REPO_ROOT / "roles" / "common" / "templates" / "nftables.conf.j2"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'iifname "{{ interface }}" oifname "{{ interface }}" accept', firewall
        )
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

    def test_loki_is_not_probed_from_inside_a_distroless_image(self) -> None:
        """Регрессия, стоившая красной выкатки.

        У Loki стояла проба `CMD-SHELL` с wget. Образ grafana/loki собран на
        gcr.io/distroless/static: в нём нет ни шелла, ни wget — только бинарь
        loki. Проба падала при каждом запуске, контейнер навсегда оставался
        `starting`, и `up --wait` валился с «application not healthy» при
        совершенно живом Loki, который в это время спокойно компактил таблицы.

        Отсюда правило: пробу выполняет тот, у кого есть чем. Готовность Loki
        проверяется с хоста задачей роли, а не изнутри контейнера.
        """
        collector = REPO_ROOT / "roles" / "platform_observability"
        compose = yaml.safe_load(
            re.sub(
                r"{{[^\n{}]+}}",
                "fixture",
                (collector / "templates" / "compose.yml.j2").read_text(encoding="utf-8"),
            )
        )
        services = compose["services"]

        self.assertNotIn("healthcheck", services["loki"])
        # Ждать по несуществующей пробе нечего: гейт был бы вечным.
        self.assertEqual(
            services["alloy"]["depends_on"]["loki"]["condition"], "service_started"
        )
        # Проверка, заменившая пробу, обязана остаться.
        collector_tasks = (collector / "tasks" / "main.yml").read_text(encoding="utf-8")
        self.assertIn("Require the log store to be ready", collector_tasks)
        self.assertIn(
            "http://127.0.0.1:{{ platform_loki_port }}/ready", collector_tasks
        )

    @unittest.skipIf(os.environ.get("SPIRITVPN_SKIP_LIVE_DESIRED") == "1", LIVE_DESIRED_SKIP_REASON)
    def test_control_containers_may_reach_the_backend_on_the_hub(self) -> None:
        """Регрессия, найденная на тестовом заказе.

        Бэкенд опубликован на management-адресе хаба, а бот живёт в контейнере
        на мосту. Его пакет приходит в цепочку input с моста и не совпадал там
        ни с одним правилом: цепочка принимала только lo и wg0. Бот получал
        таймаут при полностью живом бэкенде, который в это же время отвечал
        `grpc_code: OK` на вызовы с самого хоста.

        Порт наружу обязан совпадать с портом из `backend_endpoint` желаемого
        состояния, а forward_ports — с портом внутри контейнера, иначе правило
        разрешает не то, что нужно, и не перестаёт быть зелёным.
        """
        rules = {}
        for name in ("bootstrap", "steady"):
            play = yaml.safe_load(
                (REPO_ROOT / "playbooks" / "platform" / f"{name}.yml").read_text(
                    encoding="utf-8"
                )
            )[0]
            rules[name] = play["vars"]["common_restricted_tcp_rules"]

        # Расхождение между bootstrap и steady означало бы хаб, у которого
        # доступ к бэкенду то появляется, то пропадает.
        self.assertEqual(rules["bootstrap"], rules["steady"])

        # Правил стало несколько: после того как оверлей перестал быть доверенным
        # целиком, вход на хабе объявляется поимённо. Поэтому правило бэкенда
        # выбирается по своему признаку — мосту, — а не по тому, что оно
        # единственное. Единственность проверяется отдельно: два правила на один
        # мост означали бы порт, открытый шире, чем думает читатель.
        bridge_rules = [
            candidate
            for candidate in rules["steady"]
            if candidate.get("interface") == "br-*"
        ]
        self.assertEqual(len(bridge_rules), 1, rules["steady"])
        rule = bridge_rules[0]

        endpoint_ports = set()
        for environment in ("develop", "prod"):
            state = validate_environment(REPO_ROOT, environment)
            endpoint = state.environment.backend_endpoint
            endpoint_ports.add(int(endpoint.rsplit(":", 1)[1]))
        self.assertEqual(set(rule["ports"]), endpoint_ports)

        container_port = re.search(
            r'"backend_container_port":\s*(\d+)',
            (REPO_ROOT / "fleetctl" / "compiler" / "control.py").read_text(
                encoding="utf-8"
            ),
        )
        self.assertIsNotNone(container_port)
        self.assertEqual(rule["forward_ports"], [int(container_port.group(1))])

    def test_grafana_datasource_uids_match_the_provisioned_file(self) -> None:
        """Перезапуск Grafana решается по тому, что она загрузила.

        Роль спрашивает у Grafana список источников и сверяет его с
        `platform_grafana_datasource_uids`. Список объявлен вторым местом, и
        разойдясь с шаблоном он молча выключил бы проверку: недостающих
        источников не окажется никогда, и Grafana перестанет перезапускаться.
        """
        collector = REPO_ROOT / "roles" / "platform_observability"
        # Шаблон открывается `{{ ansible_managed | comment }}` — выражением на
        # всю строку, которое разворачивается в комментарий, а не в значение.
        # Подставленный как скаляр, оно сделало бы документ неразбираемым.
        provisioned = yaml.safe_load(
            re.sub(
                r"{{[^\n{}]+}}",
                "fixture",
                re.sub(
                    r"(?m)^{{[^\n{}]+}}$",
                    "#",
                    (collector / "templates" / "grafana-datasources.yml.j2").read_text(
                        encoding="utf-8"
                    ),
                ),
            )
        )
        declared = yaml.safe_load(
            (collector / "defaults" / "main.yml").read_text(encoding="utf-8")
        )["platform_grafana_datasource_uids"]

        self.assertEqual(
            sorted(source["uid"] for source in provisioned["datasources"]),
            sorted(declared),
        )

        # Перезапуск обязан опираться на наблюдаемое состояние, а не только на
        # то, менял ли `template` файл в этом самом прогоне.
        collector_tasks = (collector / "tasks" / "main.yml").read_text(encoding="utf-8")
        self.assertIn("/api/frontend/settings", collector_tasks)
        self.assertIn("_platform_grafana_missing_datasources | length > 0", collector_tasks)

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
        # `bool` — фильтр Ansible, в голом Jinja2 его нет. Тот же образец, что и
        # в test_bootstrap.py: строковое "false" обязано оставаться ложью, иначе
        # проверка анонимного доступа читала бы его как истину.
        jinja.filters["bool"] = lambda value: (
            value
            if isinstance(value, bool)
            else str(value).strip().lower() in ("true", "yes", "on", "1")
        )
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
    """Successful main CI may deploy develop, and only develop.

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

    def test_automatic_deploy_is_triggered_by_successful_ci_for_the_same_commit(self) -> None:
        caller = self.load("desired-state-deploy.yml")
        trigger = caller["on"]["workflow_run"]
        self.assertEqual(trigger["workflows"], ["ci"])
        self.assertEqual(trigger["types"], ["completed"])
        self.assertEqual(trigger["branches"], ["main"])

        detect = caller["jobs"]["detect"]
        self.assertIn("workflow_run.conclusion == 'success'", detect["if"])
        self.assertIn("workflow_run.event == 'push'", detect["if"])
        checkout = detect["steps"][0]
        self.assertEqual(
            checkout["with"]["ref"],
            "${{ github.event.workflow_run.head_sha }}",
        )
        current_main_gate = detect["steps"][1]["run"]
        self.assertIn("refs/remotes/origin/main", current_main_gate)
        self.assertIn('= "$SOURCE_GIT_SHA"', current_main_gate)

        for job in ("deploy-platform", "deploy-control", "deploy-fleet"):
            self.assertEqual(
                caller["jobs"][job]["with"]["source_git_sha"],
                "${{ github.event.workflow_run.head_sha }}",
            )

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
    "platform_runner": {
        "architecture": "x64",
        "bootstrap_sha256": "04cf0be1aff4c3ec3554466c39124ca250e3effd8873bb7e8d68535aa9505d5d",
        "bootstrap_version": "2.336.0",
        "home": "/var/lib/github-runner",
        "install_dir": "/opt/actions-runner",
        "labels": ["spiritvpn-deploy"],
        "name": "spiritvpn-deploy-1",
        "repository_url": "https://github.com/SpiritTechDevelopment/Infrastructure",
        "update_policy": "github-managed",
        "user": "github-runner",
        "work_dir": "_work",
    },
    "platform_vault_node_id": "management-1",
    "platform_vault_tls_server_name": "vault.internal",
    "platform_wireguard_interface": "wg0",
    "platform_wireguard_hub_addresses": {"develop": "10.80.0.1/16", "prod": "10.82.0.1/16"},
    "platform_wireguard_hub_public_key": "",
    "platform_wireguard_environment_networks": {
        "develop": "10.80.0.0/16",
        "prod": "10.82.0.0/16",
    },
    "platform_wireguard_listen_port": 51820,
    "platform_wireguard_mtu": 1420,
    "platform_wireguard_operator_peers": [
        {"id": "operator", "public_key": PEER_PUBLIC_KEY, "allowed_ips": ["10.80.0.9/32"]}
    ],
    "platform_wireguard_runner_peers": [],
    # Именно эта тройка и завела задачу в тупик: значения есть в бандле, но на
    # захардененный хаб их до сих пор доносили руками.
    "platform_alertmanager_telegram_bot_token": "12345:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "platform_alertmanager_telegram_chat_id": "-100500",
    "platform_alertmanager_telegram_thread_id": "",
}


class PlatformRuntimeProjectionTests(unittest.TestCase):
    def load_module(self) -> object:
        path = REPO_ROOT / "scripts" / "platform-sops.py"
        spec = importlib.util.spec_from_file_location("spiritvpn_platform_projection", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_runtime_projection_is_private_and_rejects_unapplied_access_changes(self) -> None:
        module = self.load_module()
        inventory = yaml.safe_load(
            (REPO_ROOT / "tests" / "fixtures" / "platform-bootstrap" / "platform.yml").read_text(
                encoding="utf-8"
            )
        )
        module.decrypt_bundle = lambda path: (
            copy.deepcopy(inventory),
            "unused known hosts\n",
            copy.deepcopy(BUNDLE_VARIABLES),
        )
        expected = {
            key: copy.deepcopy(value)
            for key, value in BUNDLE_VARIABLES.items()
            if key != "platform_runner"
        }
        expected["platform_wireguard_public_endpoint"] = "1.1.1.1:51820"

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            applied = root / "applied.yml"
            applied.write_text(yaml.safe_dump(expected), encoding="utf-8")
            output = root / "projected.yml"
            module.materialize_runtime_variables(
                Path("ignored.sops.yml"),
                output,
                applied_runtime=applied,
            )
            self.assertEqual(yaml.safe_load(output.read_text(encoding="utf-8")), expected)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

            applied_document = copy.deepcopy(expected)
            applied_document["platform_ssh_allowed_cidrs"] = ["10.82.0.0/16"]
            applied.write_text(yaml.safe_dump(applied_document), encoding="utf-8")
            refused = root / "refused.yml"
            with self.assertRaises(module.PlatformBundleError) as raised:
                module.materialize_runtime_variables(
                    Path("ignored.sops.yml"),
                    refused,
                    applied_runtime=applied,
                )
            self.assertIn("explicitly applied access contract", str(raised.exception))
            self.assertFalse(refused.exists())

            legacy_refused = root / "legacy-refused.yml"
            with self.assertRaises(module.PlatformBundleError) as legacy_raised:
                module.materialize_runtime_variables(
                    Path("ignored.sops.yml"),
                    legacy_refused,
                    applied_runtime=None,
                    executor_listen_port=51821,
                )
            self.assertIn("installed executor listen port", str(legacy_raised.exception))
            self.assertFalse(legacy_refused.exists())

            legacy_applied = copy.deepcopy(expected)
            legacy_applied.pop("platform_wireguard_hub_public_key")
            legacy_applied.pop("platform_wireguard_runner_peers")
            applied.write_text(yaml.safe_dump(legacy_applied), encoding="utf-8")
            transitioned = root / "transitioned.yml"
            module.materialize_runtime_variables(
                Path("ignored.sops.yml"),
                transitioned,
                applied_runtime=applied,
            )
            self.assertEqual(
                yaml.safe_load(transitioned.read_text(encoding="utf-8")),
                expected,
            )

    def test_runner_plan_is_private_exact_sha_projection(self) -> None:
        module = self.load_module()
        inventory = yaml.safe_load(
            (REPO_ROOT / "tests" / "fixtures" / "platform-bootstrap" / "platform.yml").read_text(
                encoding="utf-8"
            )
        )
        variables = copy.deepcopy(BUNDLE_VARIABLES)
        variables["platform_wireguard_hub_public_key"] = HUB_PUBLIC_KEY
        variables["platform_wireguard_runner_peers"] = [
            {
                "address": "10.80.255.240/32",
                "environment": "develop",
                "id": "ci-runner",
                "interface": "wg-spirit",
                "persistent_keepalive_seconds": 25,
                "public_key": "",
            }
        ]
        module.validate_variables(variables)
        module.decrypt_bundle = lambda path: (
            copy.deepcopy(inventory),
            "unused known hosts\n",
            copy.deepcopy(variables),
        )

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "runner-plan.yml"
            module.materialize_runner_plan(
                Path("ignored.sops.yml"),
                output,
                runner_id="ci-runner",
                source_git_sha="a" * 40,
            )
            plan = yaml.safe_load(output.read_text(encoding="utf-8"))
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(plan["source_git_sha"], "a" * 40)
            self.assertEqual(plan["runner"]["id"], "ci-runner")
            self.assertEqual(plan["runner"]["public_key"], "")
            self.assertEqual(plan["hub"]["public_key"], HUB_PUBLIC_KEY)
            self.assertNotIn("mtu", plan)
            self.assertRegex(
                plan["artifacts"]["enrollment_script_sha256"],
                r"^[0-9a-f]{64}$",
            )

    def test_runner_host_plan_is_private_exact_sha_projection(self) -> None:
        module = self.load_module()
        inventory = yaml.safe_load(
            (REPO_ROOT / "tests" / "fixtures" / "platform-bootstrap" / "platform.yml").read_text(
                encoding="utf-8"
            )
        )
        module.decrypt_bundle = lambda path: (
            copy.deepcopy(inventory),
            "unused known hosts\n",
            copy.deepcopy(BUNDLE_VARIABLES),
        )

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "runner-host-plan.json"
            module.materialize_runner_host_plan(
                Path("ignored.sops.yml"),
                output,
                source_git_sha="b" * 40,
            )
            plan = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(plan["source_git_sha"], "b" * 40)
            self.assertEqual(plan["runner"], BUNDLE_VARIABLES["platform_runner"])
            self.assertRegex(
                plan["artifacts"]["bootstrap_script_sha256"],
                r"^[0-9a-f]{64}$",
            )

    def test_runner_host_bootstrap_consumes_only_the_projected_contract(self) -> None:
        script = (REPO_ROOT / "scripts" / "bootstrap-self-hosted-runner.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("--plan", script)
        self.assertIn("--mode check|apply", script)
        self.assertNotIn("--repository-url", script)
        self.assertNotIn("--runner-version", script)
        self.assertNotIn("--runner-sha256", script)
        self.assertIn("github-managed", script)
        self.assertIn("bootstrap_script_sha256", script)

    def test_runner_overlay_script_consumes_only_the_projected_contract(self) -> None:
        script = (REPO_ROOT / "scripts" / "enroll-runner-overlay.sh").read_text(
            encoding="utf-8"
        )
        base = (
            REPO_ROOT / "roles" / "platform_wireguard" / "templates" / "base.conf.j2"
        ).read_text(encoding="utf-8")
        tasks = (
            REPO_ROOT / "roles" / "platform_wireguard" / "tasks" / "main.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("--plan", script)
        self.assertIn("--mode check|apply", script)
        self.assertNotIn("--hub-endpoint", script)
        self.assertNotIn("wireguard-peer reconcile", script)
        self.assertNotRegex(script, r"10\.[0-9]+\.[0-9]+\.[0-9]+")
        self.assertIn("platform_wireguard_runner_peers", base)
        self.assertIn("Remove legacy dynamic fragments now owned by the runner contract", tasks)

    def test_fleet_bootstrap_input_is_reconciled_in_steady_state(self) -> None:
        tasks = (
            REPO_ROOT / "roles" / "platform_executor" / "tasks" / "main.yml"
        ).read_text(encoding="utf-8")
        start = tasks.index(
            "Reconcile environment fleet bootstrap inputs from reviewed platform state"
        )
        end = tasks.index("Initialize the explicit control deployment approval contract", start)
        task = tasks[start:end]
        self.assertNotIn("force: false", task)
        self.assertNotIn("deploy_mode == 'bootstrap'", task)
        self.assertIn("spiritvpn_agent_certificate_chains", task)


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
            RUNTIME_VARIABLE_KEYS = set(BUNDLE_VARIABLES) - {"platform_runner"}

            @staticmethod
            def decrypt_bundle(path: Path) -> tuple[dict, str, dict]:
                return copy.deepcopy(inventory), known_hosts, copy.deepcopy(BUNDLE_VARIABLES)

        module._load_platform_sops = lambda: _Bundle
        module._require_command = lambda command: command
        module._validate_private_key = lambda path: PEER_PUBLIC_KEY
        module._run = recorder
        original_materializer = module._materialize_platform_component_vars

        def materialize(path: Path) -> None:
            variables = {
                name: f"registry.invalid/{name}:fixture@sha256:{'a' * 64}"
                for name in (
                    "platform_vault_image",
                    "platform_prometheus_image",
                    "platform_node_exporter_image",
                    "platform_grafana_image",
                    "platform_alertmanager_image",
                    "platform_loki_image",
                    "platform_alloy_image",
                )
            }
            path.write_text(yaml.safe_dump(variables), encoding="utf-8")

        module._materialize_platform_component_vars = materialize
        with tempfile.TemporaryDirectory() as temporary:
            key = Path(temporary) / "operator.key"
            key.write_text("unused\n", encoding="utf-8")
            try:
                with contextlib.redirect_stdout(io.StringIO()):  # keep test output clean
                    module.execute(
                        bundle=Path("/does/not/matter"),
                        operator_private_key=key,
                        client_interface="spiritvpn-mgmt",
                        verify_convergence=False,
                        reuse_tunnel=reuse_tunnel,
                    )
            finally:
                module._materialize_platform_component_vars = original_materializer
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
        self.assertIn("component-vars.yml", " ".join(applied))

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
            ({"endpoint": "1.1.1.1:51821"}, "Git-owned listen port"),
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

    def test_every_runtime_bundle_variable_is_persisted_by_the_executor(self) -> None:
        """Runner-host bootstrap data must not become management runtime state."""
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
        self.assertEqual(
            persisted,
            sops.RUNTIME_VARIABLE_KEYS | {"platform_wireguard_public_endpoint"},
        )
        self.assertNotIn("platform_runner", persisted)
        defaults = yaml.safe_load(
            (REPO_ROOT / "roles" / "platform_wireguard" / "defaults" / "main.yml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(defaults["platform_wireguard_interface"], "")
        self.assertEqual(defaults["platform_wireguard_environment_networks"], {})
        self.assertEqual(defaults["platform_wireguard_listen_port"], 0)
        self.assertEqual(defaults["platform_wireguard_mtu"], 0)
        self.assertEqual(defaults["platform_wireguard_hub_public_key"], "")
        self.assertEqual(defaults["platform_wireguard_runner_peers"], [])


if __name__ == "__main__":
    unittest.main()
