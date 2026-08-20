"""Pure DNS publication projection."""

from __future__ import annotations

import ipaddress
from typing import Any

from fleetctl.model import DesiredState


def compile_dns_plan(state: DesiredState) -> dict[str, Any]:
    nodes = {node.object_id: node for node in state.nodes}
    records: list[dict[str, Any]] = []
    for instance in sorted(state.instances, key=lambda item: item.object_id):
        node = nodes[instance.logical_node]
        # Both roles are client endpoints. Entry can route a user to a linked
        # exit, while an exit also serves that fleet's users directly through
        # its local FREEDOM outbound.
        if instance.target_state != "serving":
            continue
        common = state.common_for_node(node.object_id)
        address = ipaddress.ip_address(instance.public_address)
        records.append(
            {
                "id": node.object_id,
                "logical_node_id": node.object_id,
                "instance_id": instance.object_id,
                "name": node.hostname,
                "record_type": "A" if address.version == 4 else "AAAA",
                "value": instance.public_address,
                "ttl_seconds": common.networking.dns_ttl_seconds,
                "proxied": common.networking.dns_proxied,
            }
        )
    return {
        "_notice": "GENERATED — DO NOT EDIT",
        "schema_version": 1,
        "environment": state.environment.object_id,
        "zone": state.environment.dns_zone,
        "records": records,
    }
