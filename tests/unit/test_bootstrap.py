from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


class BootstrapContourTests(unittest.TestCase):
    def test_bootstrap_and_steady_state_are_separate_playbooks(self) -> None:
        bootstrap = (REPO_ROOT / "playbooks" / "bootstrap" / "bootstrap.yml").read_text(encoding="utf-8")
        configure = (REPO_ROOT / "playbooks" / "deploy" / "configure.yml").read_text(encoding="utf-8")
        self.assertIn("spiritvpn_bootstrap", bootstrap)
        self.assertIn("bootstrap_wireguard", bootstrap)
        self.assertIn("pki_agent", bootstrap)
        self.assertNotIn("compiled_runtime", bootstrap)
        self.assertIn("compiled_runtime", configure)
        self.assertIn("node_agent", configure)
        self.assertNotIn("bootstrap_wireguard", configure)
        wireguard = (REPO_ROOT / "roles" / "bootstrap_wireguard" / "tasks" / "main.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("Require management hub reachability", wireguard)
        self.assertIn("Reconcile this node as a management-hub peer", wireguard)

    def test_bootstrap_make_target_needs_explicit_apply(self) -> None:
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn("fleet-bootstrap: fleet-ansible-check", makefile)
        self.assertIn("refusing bootstrap SSH/mutation: set APPLY=1 explicitly", makefile)
        self.assertIn("no SSH attempted (set CONNECT=1 explicitly)", makefile)

    def test_node_local_wireguard_configurator_has_valid_shell(self) -> None:
        path = REPO_ROOT / "roles" / "bootstrap_wireguard" / "templates" / "configure-wireguard.sh.j2"
        rendered = re.sub(r"{{[^\n{}]+}}", "fixture", path.read_text(encoding="utf-8"))
        self.assertNotIn("{{", rendered)
        subprocess.run(["bash", "-n"], input=rendered, text=True, check=True)

    def test_certificate_renewal_hook_has_valid_shell(self) -> None:
        path = REPO_ROOT / "roles" / "pki_agent" / "templates" / "renew-agent-certificate.sh.j2"
        rendered = re.sub(r"{{[^\n{}]+}}", "fixture", path.read_text(encoding="utf-8"))
        self.assertNotIn("{{", rendered)
        subprocess.run(["bash", "-n"], input=rendered, text=True, check=True)

    def test_runtime_reconciliation_does_not_force_container_recreation(self) -> None:
        tasks = (REPO_ROOT / "roles" / "compiled_runtime" / "tasks" / "main.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("docker", tasks)
        self.assertIn("compose", tasks)
        self.assertIn("up", tasks)
        self.assertIn("_compiled_runtime_definition.changed", tasks)
        self.assertNotIn("--force-recreate", tasks)
        self.assertNotIn("compose\n      - down", tasks)

    def test_node_agent_uses_pinned_image_persistent_state_and_node_local_pki(self) -> None:
        compose = (
            REPO_ROOT / "roles" / "compiled_runtime" / "templates" / "compose.yml.j2"
        ).read_text(encoding="utf-8")
        tasks = (REPO_ROOT / "roles" / "node_agent" / "tasks" / "main.yml").read_text(
            encoding="utf-8"
        )
        readiness = (REPO_ROOT / "playbooks" / "operations" / "readiness.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("image: {{ node_agent_image }}", compose)
        self.assertIn("network_mode: host", compose)
        self.assertIn("SPIRIT_STATE_DB_PATH", compose)
        self.assertIn("/var/lib/spirit-agent", compose)
        self.assertIn("remote_src: true", tasks)
        self.assertIn('node_agent_uid: "65532"', (
            REPO_ROOT / "roles" / "node_agent" / "defaults" / "main.yml"
        ).read_text(encoding="utf-8"))
        self.assertNotIn("ansible.builtin.slurp", tasks)
        # Readiness must probe the same overlay address the agent binds. A
        # loopback probe would pass while Prometheus saw nothing.
        self.assertIn("/health/ready", readiness)
        self.assertNotIn("http://127.0.0.1:", readiness)
        self.assertIn(
            "node_agent_http_listen.split(':')[0]\n        == spiritvpn_node_plan.instance.management_address",
            tasks,
        )
        xray_defaults = (REPO_ROOT / "roles" / "xray" / "defaults" / "main.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("RoutingService", xray_defaults)


if __name__ == "__main__":
    unittest.main()
