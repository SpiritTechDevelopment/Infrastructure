from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path


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

    def test_checked_in_bootstrap_placeholders_fail_closed(self) -> None:
        result = subprocess.run(
            [
                "python3",
                str(REPO_ROOT / "scripts" / "platform-bootstrap-check.py"),
                "--inventory",
                str(REPO_ROOT / "inventories" / "bootstrap" / "platform.yml"),
                "--known-hosts",
                str(REPO_ROOT / "inventories" / "bootstrap" / "known_hosts"),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("exactly one management host", result.stderr)

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

    def test_github_forced_command_has_one_read_only_operation(self) -> None:
        gate = REPO_ROOT / "roles" / "platform_executor" / "templates" / "spiritvpn-github-command.j2"
        subprocess.run(["bash", "-n", str(gate)], check=True)
        text = gate.read_text(encoding="utf-8")
        self.assertIn("platform-readiness)", text)
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

    def test_github_workflow_cannot_mutate_or_receive_vault_credentials(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "platform-readiness.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("platform-remote.sh", workflow)
        self.assertIn("PLATFORM_SSH_PRIVATE_KEY", workflow)
        for forbidden in ("VAULT_TOKEN", "id-token: write", "fleet-deploy", "ansible-playbook", "ssh-keyscan"):
            self.assertNotIn(forbidden, workflow)

    def test_executor_templates_have_valid_shell_after_jinja_substitution(self) -> None:
        path = REPO_ROOT / "roles" / "platform_executor" / "templates" / "spiritvpn-platform-readiness.j2"
        rendered = re.sub(r"{{[^\n{}]+}}", "fixture", path.read_text(encoding="utf-8"))
        self.assertNotIn("{{", rendered)
        subprocess.run(["bash", "-n"], input=rendered, text=True, check=True)


if __name__ == "__main__":
    unittest.main()
