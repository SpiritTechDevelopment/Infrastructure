"""Fail-closed readiness suite assembled from a compiled node plan."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .model import GateReport, GateResult, GateSpec, ProbeOutcome, ReadinessProbe


def build_gate_specs(node_plan: dict[str, Any]) -> tuple[GateSpec, ...]:
    instance = node_plan["instance"]
    logical_node = node_plan["logical_node"]
    infrastructure = node_plan["infrastructure"]
    instance_id = instance["id"]
    management_interface = infrastructure["networking"]["management"]["interface"]
    specs = [
        GateSpec("host_reachable", instance_id, {"address": instance["public_address"]}),
        GateSpec(
            "management_address_reachable",
            instance_id,
            {"address": instance["management_address"], "interface": management_interface},
        ),
        GateSpec(
            "systemd_units_active",
            instance_id,
            {
                "units": [
                    "docker.service",
                    f"wg-quick@{management_interface}.service",
                    "spiritvpn-egress-qdisc.service",
                    "spiritvpn-agent-certificate-renewal.timer",
                ]
            },
        ),
        GateSpec("xray_config_syntax", instance_id, {"path": "/opt/vpn/xray/config.json"}),
        GateSpec("xray_running", instance_id, {"compose_service": "xray"}),
        GateSpec(
            "public_port_listening",
            instance_id,
            {"address": instance["public_address"], "port": logical_node["public"]["port"]},
        ),
        GateSpec("cake_policy", instance_id, instance["bandwidth"]),
        GateSpec(
            "node_exporter_metrics",
            instance_id,
            {
                "address": "127.0.0.1",
                "port": infrastructure["observability"]["ports"]["node_exporter"],
                "path": "/metrics",
            },
        ),
        GateSpec(
            "machine_certificate",
            instance_id,
            {
                "path": "/var/lib/spiritvpn/pki/agent-chain.pem",
                "identity": instance["agent"]["certificate_identity"],
                "minimum_validity_seconds": 604800,
            },
        ),
    ]
    if logical_node["role"] == "exit":
        specs.append(
            GateSpec(
                "direct_exit_smoke",
                instance_id,
                {
                    "address": instance["public_address"],
                    "port": logical_node["public"]["port"],
                    "server_name": logical_node["public"]["server_name"],
                },
            )
        )
    for bridge in node_plan["routing"]["bridges_as_entry"]:
        specs.append(
            GateSpec(
                "entry_to_exit_smoke",
                instance_id,
                {
                    "routing_key": bridge["routing_key"],
                    "egress_tag": bridge["egress_tag"],
                    "target_instance_id": bridge["target"]["instance_id"],
                    "target_address": bridge["target"]["address"],
                },
            )
        )
    return tuple(specs)


class GateRunner:
    def __init__(self, clock: Callable[[], datetime] | None = None):
        self.clock = clock or (lambda: datetime.now(UTC))

    def run(
        self,
        node_plan: dict[str, Any],
        probe: ReadinessProbe,
        *,
        timeout_seconds: int,
    ) -> GateReport:
        if timeout_seconds <= 0:
            raise ValueError("readiness timeout must be positive")
        results: list[GateResult] = []
        for gate in build_gate_specs(node_plan):
            try:
                outcome = probe.execute(gate, timeout_seconds=timeout_seconds)
                if not isinstance(outcome, ProbeOutcome):
                    raise TypeError("probe returned an unsupported outcome")
                passed = outcome.passed
                diagnostic = outcome.diagnostic.strip() or "probe returned no diagnostic"
            except TimeoutError as exc:
                passed = False
                diagnostic = f"timeout is a failure: {exc or 'probe deadline exceeded'}"
            except Exception as exc:  # adapters must never turn unexpected errors into success
                passed = False
                diagnostic = f"probe error: {type(exc).__name__}: {exc}"
            timestamp = self.clock().astimezone(UTC).isoformat().replace("+00:00", "Z")
            results.append(
                GateResult(
                    name=gate.name,
                    instance_id=gate.instance_id,
                    passed=passed,
                    diagnostic=diagnostic,
                    timestamp=timestamp,
                )
            )
        return GateReport(
            environment=node_plan["environment"],
            instance_id=node_plan["instance"]["id"],
            results=tuple(results),
        )
