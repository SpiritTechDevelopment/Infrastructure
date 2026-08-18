from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


class NodeLimitsRoleTests(unittest.TestCase):
    def test_qdisc_unit_is_started_because_readiness_asks_if_it_is_active(self) -> None:
        """`enabled` alone leaves a RemainAfterExit unit forever inactive.

        playbooks/bootstrap/readiness.yml runs `systemctl is-active` on this
        unit, so a role that only enables it fails bootstrap on a node whose
        qdisc is exactly right.
        """
        tasks = (REPO_ROOT / "roles" / "node_limits" / "tasks" / "main.yml").read_text(
            encoding="utf-8"
        )
        readiness = (REPO_ROOT / "playbooks" / "bootstrap" / "readiness.yml").read_text(
            encoding="utf-8"
        )
        unit_template = (
            REPO_ROOT / "roles" / "node_limits" / "templates"
            / "spiritvpn-egress-qdisc.service.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("is-active", readiness)
        self.assertIn("spiritvpn-egress-qdisc.service", readiness)
        self.assertIn("RemainAfterExit=yes", unit_template)
        self.assertIn("state: started", tasks)

    def test_rtt_readback_tolerates_both_iproute2_spellings(self) -> None:
        """iproute2 prints `rtt 100ms` on 22.04 and `rtt 100.0ms` on 24.04.

        The assertion must check the policy, not the formatting of someone
        else's tool — a literal match failed bootstrap on entry-ru while the
        qdisc was in fact exactly what desired state asked for.
        """
        tasks = (REPO_ROOT / "roles" / "node_limits" / "tasks" / "main.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("search('rtt 100(\\\\.0)?ms')", tasks)
        self.assertNotIn("'rtt 100.0ms' in", tasks)

        pattern = re.compile(r"rtt 100(\.0)?ms")
        for accepted in ("... split-gso rtt 100ms raw", "... split-gso rtt 100.0ms raw"):
            self.assertIsNotNone(pattern.search(accepted), accepted)
        # Must not degrade into accepting any rtt at all.
        for rejected in ("rtt 10ms raw", "rtt 1000ms raw", "rtt 100s raw"):
            self.assertIsNone(pattern.search(rejected), rejected)


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
