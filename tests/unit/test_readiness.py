from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from pathlib import Path

from fleetctl.compiler import compile_node_plans
from fleetctl.readiness import GateRunner, ProbeOutcome, build_gate_specs
from fleetctl.validation import validate_environment


REPO_ROOT = Path(__file__).resolve().parents[2]
VALID_DESIRED = REPO_ROOT / "tests" / "fixtures" / "valid" / "desired"


class PassingProbe:
    def execute(self, gate: object, *, timeout_seconds: int) -> ProbeOutcome:
        return ProbeOutcome(True, f"completed within {timeout_seconds}s")


class TimeoutProbe:
    def execute(self, gate: object, *, timeout_seconds: int) -> ProbeOutcome:
        raise TimeoutError(f"exceeded {timeout_seconds}s")


class SelectiveProbe:
    def __init__(self, failed_gate: str):
        self.failed_gate = failed_gate

    def execute(self, gate: object, *, timeout_seconds: int) -> ProbeOutcome:
        if gate.name == self.failed_gate:
            return ProbeOutcome(False, "expected state was not observed")
        return ProbeOutcome(True, "ok")


class ReadinessGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        state = validate_environment(REPO_ROOT, "develop", desired_root=VALID_DESIRED)
        cls.plans = compile_node_plans(state)
        cls.clock = staticmethod(lambda: datetime(2026, 8, 12, 12, 0, tzinfo=UTC))

    def test_required_gates_are_assembled_from_exit_plan(self) -> None:
        names = {gate.name for gate in build_gate_specs(self.plans["develop-exit-de-01"])}
        self.assertTrue(
            {
                "host_reachable",
                "management_address_reachable",
                "systemd_units_active",
                "xray_config_syntax",
                "xray_running",
                "public_port_listening",
                "cake_policy",
                "node_exporter_metrics",
                "machine_certificate",
                "direct_exit_smoke",
            }.issubset(names)
        )

    def test_entry_plan_exposes_entry_to_exit_smoke_interface(self) -> None:
        specs = build_gate_specs(self.plans["develop-entry-nl-01"])
        bridge = next(gate for gate in specs if gate.name == "entry_to_exit_smoke")
        self.assertEqual(bridge.parameters["routing_key"], "develop-entry-nl.to-develop-exit-de")
        self.assertEqual(bridge.parameters["target_instance_id"], "develop-exit-de-01")

    def test_structured_success_report_has_diagnostics_and_timestamps(self) -> None:
        report = GateRunner(self.clock).run(
            self.plans["develop-exit-de-01"],
            PassingProbe(),
            timeout_seconds=10,
        )
        payload = report.to_dict()
        json.dumps(payload)
        self.assertTrue(report.passed)
        self.assertTrue(all(result.diagnostic for result in report.results))
        self.assertTrue(all(result.timestamp == "2026-08-12T12:00:00Z" for result in report.results))

    def test_timeout_is_failure_for_every_gate(self) -> None:
        report = GateRunner(self.clock).run(
            self.plans["develop-exit-de-01"],
            TimeoutProbe(),
            timeout_seconds=3,
        )
        self.assertFalse(report.passed)
        self.assertTrue(all(not result.passed for result in report.results))
        self.assertTrue(all("timeout is a failure" in result.diagnostic for result in report.results))

    def test_one_failed_gate_fails_whole_report(self) -> None:
        report = GateRunner(self.clock).run(
            self.plans["develop-entry-nl-01"],
            SelectiveProbe("cake_policy"),
            timeout_seconds=10,
        )
        self.assertFalse(report.passed)
        cake = next(result for result in report.results if result.name == "cake_policy")
        self.assertFalse(cake.passed)
        self.assertEqual(cake.diagnostic, "expected state was not observed")


if __name__ == "__main__":
    unittest.main()
