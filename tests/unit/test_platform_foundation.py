from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]


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
            "environment: ${{ inputs.environment }}",
            "secrets.PLATFORM_SSH_HOST",
            "secrets.PLATFORM_SSH_KNOWN_HOSTS",
            "runs-on: [self-hosted, linux, spiritvpn-deploy]",
        ):
            self.assertIn(required, workflow)
        self.assertNotIn("vars.PLATFORM_SSH_HOST", workflow)
        self.assertNotIn("inventories/bootstrap/known_hosts", workflow)
        for forbidden in ("VAULT_TOKEN", "id-token: write", "ssh-keyscan", "update-deployment-ref"):
            self.assertNotIn(forbidden, workflow)

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
            "environment: ${{ inputs.environment }}",
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

    def test_root_executor_is_fail_closed_and_does_not_move_deployment_ref(self) -> None:
        path = REPO_ROOT / "roles" / "platform_executor" / "templates" / "spiritvpn-fleet-deploy.j2"
        text = path.read_text(encoding="utf-8")
        for required in (
            'bundle verify "$bundle"',
            "refs/spiritvpn/source",
            "vault-secret-resolver.py",
            "StrictHostKeyChecking=yes",
            "--state-dir",
        ):
            self.assertIn(required, text)
        self.assertNotIn("update-deployment-ref", text)
        self.assertNotIn("eval", text)

    def test_vault_operator_is_manual_and_environment_scoped(self) -> None:
        operator = (
            REPO_ROOT / "roles" / "platform_vault" / "templates" / "spiritvpn-vault-operator.j2"
        ).read_text(encoding="utf-8")
        policy = (
            REPO_ROOT / "roles" / "platform_vault" / "templates" / "policy-fleet-deployer.hcl.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("operator init", operator)
        self.assertIn("operator unseal", operator)
        self.assertIn("secret_id_bound_cidrs=127.0.0.1/32", operator)
        self.assertIn("token_no_default_policy=true", operator)
        self.assertNotIn('-e "VAULT_TOKEN=$root_token"', operator)
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
            REPO_ROOT / "roles" / "platform_vault" / "templates" / "spiritvpn-vault-operator.j2",
            REPO_ROOT / "roles" / "platform_wireguard" / "templates" / "spiritvpn-wireguard-reconcile.j2",
            REPO_ROOT / "roles" / "platform_wireguard" / "templates" / "spiritvpn-wireguard-peer.j2",
        )
        for path in paths:
            rendered = re.sub(r"{{[^\n{}]+}}", "fixture", path.read_text(encoding="utf-8"))
            self.assertNotIn("{{", rendered)
            subprocess.run(["bash", "-n"], input=rendered, text=True, check=True)


if __name__ == "__main__":
    unittest.main()
