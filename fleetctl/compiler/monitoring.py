"""Чистая проекция целей мониторинга."""

from __future__ import annotations

import ipaddress
from typing import Any

from fleetctl.model import DesiredState

from .addressing import (
    CONTROL_BACKEND_METRICS_PORT,
    CONTROL_POSTGRES_METRICS_PORT,
    agent_tls_server_name,
    control_management_address,
    management_address,
)


class MonitoringPlanError(ValueError):
    """Проекция содержит недоступную или небезопасную цель."""


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
        fleet_id = fleets_by_node.get(node.object_id)
        labels = {
            "environment": state.environment.object_id,
            "fleet": fleet_id or "unassigned",
            "logical_node": node.object_id,
            "instance": instance.object_id,
            "role": node.role,
            "region": node.region,
            "lifecycle": instance.target_state,
        }
        target_state = {
            "slo_eligible": instance.target_state == "serving" and fleet_id is not None,
            "readiness_expected": instance.target_state in {"candidate", "serving", "draining"},
        }
        targets.extend(
            (
                {
                    "id": f"{instance.object_id}:node-exporter",
                    "service": "node-exporter",
                    "kind": "metrics",
                    "collection": "management",
                    "endpoint": {
                        "scheme": "http",
                        "address": address,
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
                    # Xray отдаёт expvar, поэтому без экспортера метрики остаются локальными.
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
                    "id": f"{instance.object_id}:agent-metrics",
                    "service": "agent-metrics",
                    "kind": "metrics",
                    "collection": "management",
                    "endpoint": {
                        "scheme": "http",
                        "address": address,
                        "port": common.observability.agent_metrics_port,
                        "path": "/metrics",
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
    targets.extend(_control_targets(state))
    _assert_scraped_targets_stay_on_the_overlay(state, targets)
    return {
        "_notice": "GENERATED — DO NOT EDIT",
        "schema_version": 1,
        "environment": state.environment.object_id,
        "targets": sorted(targets, key=lambda item: item["id"]),
    }


def _assert_scraped_targets_stay_on_the_overlay(
    state: DesiredState, targets: list[dict[str, Any]]
) -> None:
    """Проверяет, что Prometheus обращается к нодам только через WireGuard."""
    network = ipaddress.ip_network(state.environment.management_network, strict=True)
    for target in targets:
        if target["kind"] != "metrics" or target["collection"] != "management":
            continue
        address = ipaddress.ip_address(target["endpoint"]["address"])
        if address not in network:
            raise MonitoringPlanError(
                f"scrape target {target['id']} resolves to {address}, "
                f"which is outside the management network {network}"
            )


def _control_targets(state: DesiredState) -> list[dict[str, Any]]:
    """Формирует цели метрик control на управляющем хосте."""
    if state.environment.control is None:
        return []
    environment = state.environment.object_id
    labels = {
        "environment": environment,
        "fleet": "control",
        "logical_node": "control",
        "instance": f"control-{environment}",
        "role": "control",
        "region": "management",
        "lifecycle": "serving",
    }
    address = control_management_address(state.environment)
    return [
        {
            "id": f"control-{environment}:backend-metrics",
            "service": "backend-metrics",
            "kind": "metrics",
            "collection": "management",
            "endpoint": {
                "scheme": "http",
                "address": address,
                "port": CONTROL_BACKEND_METRICS_PORT,
                "path": "/metrics",
            },
            "labels": labels,
            "slo_eligible": True,
            "readiness_expected": True,
        },
        {
            "id": f"control-{environment}:postgres-metrics",
            "service": "postgres-metrics",
            "kind": "metrics",
            "collection": "management",
            "endpoint": {
                "scheme": "http",
                "address": address,
                "port": CONTROL_POSTGRES_METRICS_PORT,
                "path": "/metrics",
            },
            "labels": labels,
            # Сбой экспортера БД ухудшает наблюдаемость, но не доступность сервиса.
            "slo_eligible": False,
            "readiness_expected": True,
        },
    ]
