from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


class NodeLimitsRoleTests(unittest.TestCase):
    def test_cake_reconciler_template_is_valid_shell(self) -> None:
        template = (
            REPO_ROOT / "roles" / "node_limits" / "templates" / "apply-egress-qdisc.sh.j2"
        ).read_text(encoding="utf-8")
        rendered = template
        for source, target in {
            "{{ node_limits_resolved_egress_interface }}": "eth0",
            "{{ node_limits_egress_limit_mbps }}": "900",
            "{{ node_limits_diffserv }}": "besteffort",
            "{{ node_limits_flow_isolation }}": "dual-dsthost",
            '{{ "nat" if (node_limits_nat | bool) else "nonat" }}': "nonat",
            "{{ node_limits_rtt }}": "internet",
        }.items():
            rendered = rendered.replace(source, target)
        self.assertNotIn("{{", rendered)
        subprocess.run(["bash", "-n"], input=rendered, text=True, check=True)

    def test_role_is_wired_into_compiled_deployment(self) -> None:
        content = (REPO_ROOT / "playbooks" / "deploy" / "configure.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("role: node_limits", content)
        self.assertIn("hosts: spiritvpn_fleet", content)

    def test_readiness_checks_cake_without_tuning_kernel_or_nofile(self) -> None:
        content = (
            REPO_ROOT / "playbooks" / "operations" / "readiness.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("tc, -s, qdisc, show", content)
        self.assertIn("qdisc cake", content)
        self.assertIn("egress_limit_mbps", content)
        self.assertIn("nf_conntrack_count", content)
        self.assertIn("nf_conntrack_max", content)
        self.assertIn("Max open files", content)
        self.assertNotIn("ansible.posix.sysctl", content)
        self.assertNotIn("ulimits:", content)


if __name__ == "__main__":
    unittest.main()
