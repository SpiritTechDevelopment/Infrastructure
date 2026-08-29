"""Чистая проекция инфраструктурного плана для каждого инстанса."""

from __future__ import annotations

from typing import Any

from fleetctl.model import DesiredState, Fleet, Instance, LogicalNode, XrayConfig, common_values

from .addressing import (
    PLATFORM_LOKI_PORT,
    agent_certificate_identity,
    agent_endpoint,
    agent_tls_server_name,
    control_management_address,
    management_address,
)


def compile_node_plans(state: DesiredState) -> dict[str, dict[str, Any]]:
    nodes = {node.object_id: node for node in state.nodes}
    fleets_by_node = _fleets_by_node(state)
    serving_instances = {
        instance.logical_node: instance
        for instance in state.instances
        if instance.target_state == "serving"
    }
    plans: dict[str, dict[str, Any]] = {}
    for instance in sorted(state.instances, key=lambda item: item.object_id):
        node = nodes[instance.logical_node]
        common = state.common_for_node(node.object_id)
        fleet = fleets_by_node.get(node.object_id)
        bandwidth = common.limits.bandwidth_profiles[instance.bandwidth_profile]
        address = management_address(state.environment, node, instance)
        plans[instance.object_id] = {
            "_notice": "GENERATED — DO NOT EDIT",
            "schema_version": 1,
            "environment": state.environment.object_id,
            "infrastructure": common_values(common),
            "fleet": {
                "id": fleet.object_id if fleet is not None else None,
                "vpn_fleet_id": state.fleet_ids[fleet.object_id] if fleet is not None else None,
            },
            "logical_node": {
                "id": node.object_id,
                "role": node.role,
                "region": node.region,
                "display_name": node.display_name,
                "public": {
                    "hostname": node.hostname,
                    "port": node.public_port,
                    "transport": node.transport,
                    "flow": node.flow,
                    "fingerprint": node.fingerprint,
                    "server_name": node.server_name,
                    **_xhttp_values(node),
                },
                "reality": {
                    "public_key": node.reality_public_key,
                    "short_id": node.reality_short_id,
                    "private_key_ref": node.private_key_ref,
                },
                "mask": {
                    "certificate_ref": node.mask_certificate_ref,
                    "private_key_ref": node.mask_private_key_ref,
                },
            },
            "instance": {
                "id": instance.object_id,
                "target_state": instance.target_state,
                "public_address": instance.public_address,
                "management_address": address,
                "management_network": state.environment.management_network,
                "bandwidth": {
                    "profile": instance.bandwidth_profile,
                    "port_capacity_mbps": bandwidth.port_capacity_mbps,
                    "egress_limit_mbps": bandwidth.egress_limit_mbps,
                    "qdisc": {
                        "kind": bandwidth.qdisc_kind,
                        "diffserv": bandwidth.diffserv,
                        "flow_isolation": bandwidth.flow_isolation_for(node.role),
                        "nat": bandwidth.nat,
                        "rtt": bandwidth.rtt,
                    },
                },
                "agent": {
                    "endpoint": agent_endpoint(
                        state.environment,
                        node,
                        instance,
                        common.networking.agent_port,
                    ),
                    "tls_server_name": agent_tls_server_name(state.environment, instance),
                    "certificate_identity": agent_certificate_identity(state.environment, instance),
                },
                "provider": {
                    "name": instance.provider_name,
                    "resource_id": instance.provider_resource_id,
                },
            },
            "routing": _routing_projection(
                fleet,
                node,
                nodes,
                serving_instances,
                common.xray,
            ),
            "logs": _logs_projection(state),
        }
    return plans


def _xhttp_values(node: LogicalNode) -> dict[str, Any]:
    """Блок xhttp попадает в план только у XHTTP-ноды."""

    if node.xhttp is None:
        return {}
    return {"xhttp": {"path": node.xhttp.path, "mode": node.xhttp.mode}}


def _logs_projection(state: DesiredState) -> dict[str, Any]:
    """Задаёт безопасную отправку логов на overlay-адрес хаба без access-log Xray."""
    return {
        "endpoint": (
            f"http://{control_management_address(state.environment)}"
            f":{PLATFORM_LOKI_PORT}/loki/api/v1/push"
        ),
        "xray_access_log_exported": False,
    }


def _fleets_by_node(state: DesiredState) -> dict[str, Fleet]:
    result: dict[str, Fleet] = {}
    for fleet in state.fleets:
        for node_id in (*fleet.entries, *fleet.exits):
            result[node_id] = fleet
    return result


def _routing_projection(
    fleet: Fleet | None,
    node: LogicalNode,
    nodes: dict[str, LogicalNode],
    serving_instances: dict[str, Instance],
    xray: XrayConfig,
) -> dict[str, Any]:
    as_entry: list[dict[str, Any]] = []
    as_exit: list[dict[str, Any]] = []
    egress_table: dict[str, str] = {"": xray.direct_outbound_tag}
    if fleet is None:
        return {
            "egress_table": egress_table,
            "bridges_as_entry": as_entry,
            "bridges_as_exit": as_exit,
        }
    for bridge in sorted(fleet.bridges, key=lambda item: item.routing_key):
        if bridge.entry == node.object_id:
            exit_node = nodes[bridge.exit]
            exit_instance = serving_instances[exit_node.object_id]
            tag = f"{xray.exit_outbound_prefix}{exit_node.object_id}"
            egress_table[tag] = tag
            as_entry.append(
                {
                    "routing_key": bridge.routing_key,
                    "service_credential_ref": bridge.service_credential_ref,
                    "exit_node_id": exit_node.object_id,
                    "egress_tag": tag,
                    "target": {
                        "instance_id": exit_instance.object_id,
                        "address": exit_instance.public_address,
                        "port": exit_node.public_port,
                        "server_name": exit_node.server_name,
                        "reality_public_key": exit_node.reality_public_key,
                        "short_id": exit_node.reality_short_id,
                        "fingerprint": exit_node.fingerprint,
                        "flow": exit_node.flow,
                        "transport": exit_node.transport,
                        **_xhttp_values(exit_node),
                    },
                }
            )
        if bridge.exit == node.object_id:
            as_exit.append(
                {
                    "routing_key": bridge.routing_key,
                    "service_credential_ref": bridge.service_credential_ref,
                    "entry_node_id": bridge.entry,
                    "egress_tag": f"{xray.exit_outbound_prefix}{node.object_id}",
                }
            )
    return {
        "egress_table": egress_table,
        "bridges_as_entry": as_entry,
        "bridges_as_exit": as_exit,
    }
