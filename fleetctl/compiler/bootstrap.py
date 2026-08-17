"""Minimal public-address inventory for first-time VPS bootstrap."""

from __future__ import annotations

from typing import Any

from fleetctl.model import DesiredState


def compile_bootstrap_inventory(state: DesiredState) -> dict[str, Any]:
    hosts: dict[str, dict[str, Any]] = {}
    for instance in sorted(state.instances, key=lambda item: item.object_id):
        if instance.target_state == "retired":
            continue
        hosts[instance.object_id] = {
            "ansible_host": instance.public_address,
            "ansible_user": "root",
            "spiritvpn_connection_phase": "bootstrap",
            "spiritvpn_node_plan_file": f"node-plans/{instance.object_id}.json",
        }
    return {"all": {"children": {"spiritvpn_bootstrap": {"hosts": hosts}}}}
