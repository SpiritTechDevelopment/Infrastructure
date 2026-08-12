"""Pure monitoring discovery projection."""

from __future__ import annotations

from typing import Any

from fleetctl.model import DesiredState

from .addressing import agent_tls_server_name, management_address


def compile_monitoring_targets(state: DesiredState) -> dict[str, Any]:
    nodes = {node.object_id: node for node in state.nodes}
    fleets_by_node = {
        node_id: fleet.object_id
        for fleet in state.fleets
        for node_id in (*fleet.entries, *fleet.exits)
    }
    targets: list[dict[str, Any]] = []
    for instance in sorted(state.instances, key=lambda item: item.object_id):
        if instance.target_state == "retired":
            continue
        node = nodes[instance.logical_node]
        common = state.common_for_node(node.object_id)
        address = management_address(state.environment, node, instance)
        labels = {
            "environment": state.environment.object_id,
            "fleet": fleets_by_node[node.object_id],
            "logical_node": node.object_id,
            "instance": instance.object_id,
            "role": node.role,
            "region": node.region,
            "lifecycle": instance.target_state,
        }
        target_state = {
            "slo_eligible": instance.target_state == "serving",
            "readiness_expected": instance.target_state in {"candidate", "serving", "draining"},
        }
        targets.extend(
            (
                {
                    "id": f"{instance.object_id}:node-exporter",
                    "service": "node-exporter",
                    "kind": "metrics",
                    "collection": "node-local",
                    "endpoint": {
                        "scheme": "http",
                        "address": "127.0.0.1",
                        "port": common.observability.node_exporter_port,
                        "path": "/metrics",
                    },
                    "labels": labels,
                    **target_state,
                },
                {
                    "id": f"{instance.object_id}:xray-metrics",
                    "service": "xray-metrics",
                    "kind": "metrics",
                    "collection": "node-local",
                    "endpoint": {
                        "scheme": "http",
                        "address": "127.0.0.1",
                        "port": common.observability.xray_metrics_port,
                        "path": "/debug/vars",
                    },
                    "labels": labels,
                    **target_state,
                },
                {
                    "id": f"{instance.object_id}:agent",
                    "service": "agent",
                    "kind": "health",
                    "collection": "management",
                    "endpoint": {
                        "protocol": "grpc",
                        "address": address,
                        "port": common.networking.agent_port,
                        "tls_server_name": agent_tls_server_name(state.environment, instance),
                    },
                    "labels": labels,
                    **target_state,
                },
                {
                    "id": f"{instance.object_id}:public-vless",
                    "service": "public-vless",
                    "kind": "probe",
                    "collection": "external",
                    "endpoint": {
                        "protocol": node.transport,
                        "address": instance.public_address,
                        "port": node.public_port,
                        "hostname": node.hostname,
                        "server_name": node.server_name,
                    },
                    "labels": labels,
                    **target_state,
                },
            )
        )
    return {
        "_notice": "GENERATED — DO NOT EDIT",
        "schema_version": 1,
        "environment": state.environment.object_id,
        "targets": sorted(targets, key=lambda item: item["id"]),
    }
