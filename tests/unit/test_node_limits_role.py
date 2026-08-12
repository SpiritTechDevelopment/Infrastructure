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

    def test_role_is_wired_into_all_traffic_deploy_playbooks(self) -> None:
        for name in ("fleet-infra.yml", "fleet-entries.yml", "fleet-exits.yml"):
            with self.subTest(playbook=name):
                content = (REPO_ROOT / "playbooks" / name).read_text(encoding="utf-8")
                self.assertIn("role: node_limits", content)

    def test_verify_reports_capacity_without_tuning_kernel_or_nofile(self) -> None:
        content = (REPO_ROOT / "playbooks" / "verify.yml").read_text(encoding="utf-8")
        self.assertIn("nf_conntrack_count", content)
        self.assertIn("nf_conntrack_max", content)
        self.assertIn("Max open files", content)
        self.assertIn("tc, -s, qdisc", content)
        self.assertNotIn("ansible.posix.sysctl", content)
        self.assertNotIn("ulimits:", content)


if __name__ == "__main__":
    unittest.main()
