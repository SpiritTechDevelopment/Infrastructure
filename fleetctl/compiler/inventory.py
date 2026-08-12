"""Pure Ansible inventory projection."""

from __future__ import annotations

from typing import Any

from fleetctl.model import DesiredState

from .addressing import (
    agent_certificate_identity,
    agent_endpoint,
    agent_tls_server_name,
    management_address,
)


LIFECYCLE_STATES = ("provisioning", "candidate", "serving", "draining", "retired")


def compile_ansible_inventory(state: DesiredState) -> dict[str, Any]:
    nodes = {node.object_id: node for node in state.nodes}
    role_hosts: dict[str, dict[str, Any]] = {"entry": {}, "exit": {}}
    lifecycle_hosts: dict[str, dict[str, Any]] = {name: {} for name in LIFECYCLE_STATES}

    for instance in sorted(state.instances, key=lambda item: item.object_id):
        node = nodes[instance.logical_node]
        common = state.common_for_node(node.object_id)
        bandwidth = common.limits.bandwidth_profiles[instance.bandwidth_profile]
        address = management_address(state.environment, node, instance)
        hostvars = {
            "ansible_host": address,
            "spiritvpn_agent_certificate_identity": agent_certificate_identity(state.environment, instance),
            "spiritvpn_agent_endpoint": agent_endpoint(
                state.environment,
                node,
                instance,
                common.networking.agent_port,
            ),
            "spiritvpn_agent_tls_server_name": agent_tls_server_name(state.environment, instance),
            "spiritvpn_environment": state.environment.object_id,
            "spiritvpn_instance_id": instance.object_id,
            "spiritvpn_logical_node_id": node.object_id,
            "spiritvpn_management_address": address,
            "spiritvpn_management_interface": common.networking.management_interface,
            "spiritvpn_management_listen_port": common.networking.management_listen_port,
            "spiritvpn_management_mtu": common.networking.management_mtu,
            "spiritvpn_management_persistent_keepalive_seconds": (
                common.networking.persistent_keepalive_seconds
            ),
            "spiritvpn_provider_name": instance.provider_name,
            "spiritvpn_provider_resource_id": instance.provider_resource_id,
            "spiritvpn_public_address": instance.public_address,
            "spiritvpn_region": node.region,
            "spiritvpn_role": node.role,
            "spiritvpn_target_state": instance.target_state,
            "node_limits_egress_enabled": instance.target_state != "retired",
            "node_limits_bandwidth_profile": instance.bandwidth_profile,
            "node_limits_port_capacity_mbps": bandwidth.port_capacity_mbps,
            "node_limits_egress_limit_mbps": bandwidth.egress_limit_mbps,
            "node_limits_qdisc_kind": bandwidth.qdisc_kind,
            "node_limits_diffserv": bandwidth.diffserv,
            "node_limits_flow_isolation": bandwidth.flow_isolation_for(node.role),
            "node_limits_nat": bandwidth.nat,
            "node_limits_rtt": bandwidth.rtt,
        }
        role_hosts[node.role][instance.object_id] = hostvars
        lifecycle_hosts[instance.target_state][instance.object_id] = {}

    children: dict[str, Any] = {
        "spiritvpn_fleet": {
            "children": {
                "entry": {"hosts": role_hosts["entry"]},
                "exit": {"hosts": role_hosts["exit"]},
            }
        }
    }
    for lifecycle, hosts in lifecycle_hosts.items():
        children[f"instance_{lifecycle}"] = {"hosts": hosts}
    return {"all": {"children": children}}
