"""Чистая проекция Ansible inventory."""

from __future__ import annotations

from typing import Any

from fleetctl.model import DesiredState

from .addressing import management_address


LIFECYCLE_STATES = ("provisioning", "candidate", "serving", "draining", "retired")


def compile_ansible_inventory(state: DesiredState) -> dict[str, Any]:
    nodes = {node.object_id: node for node in state.nodes}
    role_hosts: dict[str, dict[str, Any]] = {"entry": {}, "exit": {}}
    lifecycle_hosts: dict[str, dict[str, Any]] = {name: {} for name in LIFECYCLE_STATES}

    for instance in sorted(state.instances, key=lambda item: item.object_id):
        node = nodes[instance.logical_node]
        lifecycle_hosts[instance.target_state][instance.object_id] = {}
        if instance.target_state == "retired":
            continue
        address = management_address(state.environment, node, instance)
        # После бутстрапа нода доступна на объявленном порту sshd.
        hostvars = {
            "ansible_host": address,
            "ansible_port": state.common_for_node(node.object_id).networking.ssh_port,
            "spiritvpn_node_plan_file": f"node-plans/{instance.object_id}.json",
        }
        role_hosts[node.role][instance.object_id] = hostvars

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
