from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

import yaml
from jinja2 import Environment


REPO_ROOT = Path(__file__).resolve().parents[2]


def compiled_node_facts() -> dict[str, object]:
    """The set_fact bodies of roles/compiled_node_plan, merged."""
    tasks = yaml.safe_load(
        (REPO_ROOT / "roles" / "compiled_node_plan" / "tasks" / "main.yml").read_text(
            encoding="utf-8"
        )
    )
    facts: dict[str, object] = {}
    for task in tasks:
        block = task.get("ansible.builtin.set_fact")
        if isinstance(block, dict):
            facts.update(block)
    return facts


class XrayAccessLogTests(unittest.TestCase):
    """The access log holds client addresses, destinations and the user.

    Desired state carries `xray.access_log.enabled`, and for a while nothing
    read it: the log path was set unconditionally, so turning the flag off
    changed nothing at all.
    """

    def render_access_log_path(self, *, enabled: bool) -> str:
        expression = compiled_node_facts()["xray_access_log"]
        environment = Environment()
        # Ansible's `bool`, which stock Jinja does not provide.
        environment.filters["bool"] = lambda value: (
            value
            if isinstance(value, bool)
            else str(value).strip().lower() in ("true", "yes", "on", "1")
        )
        return environment.from_string(str(expression)).render(
            spiritvpn_node_plan={
                "infrastructure": {"xray": {"access_log": {"enabled": enabled}}}
            }
        )

    def test_disabled_access_log_is_none_and_never_an_empty_string(self) -> None:
        # An empty string does not disable the log in Xray, it redirects it to
        # stdout — where the json-file driver keeps writing it to disk anyway.
        self.assertEqual(self.render_access_log_path(enabled=False), "none")
        self.assertEqual(
            self.render_access_log_path(enabled=True), "/var/log/xray/access.log"
        )

    def test_role_default_does_not_fall_back_to_stdout(self) -> None:
        defaults = yaml.safe_load(
            (REPO_ROOT / "roles" / "xray" / "defaults" / "main.yml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(defaults["xray_access_log"], "none")

    def test_repository_desired_state_keeps_the_access_log_off(self) -> None:
        common = yaml.safe_load(
            (REPO_ROOT / "desired" / "common" / "xray.yml").read_text(encoding="utf-8")
        )
        self.assertFalse(common["access_log"]["enabled"])
        self.assertFalse(common["access_log"]["export_enabled"])

    def test_export_stays_forbidden_while_enabling_became_a_choice(self) -> None:
        """Only half of the old pair was removed, and deliberately so."""
        semantic = (
            REPO_ROOT / "fleetctl" / "validation" / "semantic.py"
        ).read_text(encoding="utf-8")
        self.assertIn("ACCESS_LOG_EXPORT", semantic)
        self.assertNotIn("ACCESS_LOG_REQUIRED", semantic)

    def test_enabling_the_access_log_still_validates(self) -> None:
        """Turning it on for an investigation must remain possible."""
        from fleetctl.validation import validate_environment

        state = validate_environment(
            REPO_ROOT,
            "develop",
            desired_root=REPO_ROOT / "tests" / "fixtures" / "valid" / "desired",
        )
        self.assertTrue(state.common.xray.access_log_enabled)
        self.assertFalse(state.common.xray.access_log_export_enabled)


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
