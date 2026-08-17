"""Pure semantic diff and impact expansion."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, Iterable

from fleetctl.model import DesiredState

from .canonical import canonical_digest, canonical_state
from .model import ImpactPlan, PlanningError


AFFECTED_KEYS = (
    "control",
    "provision",
    "configure",
    "drain",
    "retire",
    "backend_nodes",
    "node_runtime",
    "dns_nodes",
    "monitoring",
    "management",
)


def build_impact_plan(
    current: DesiredState,
    baseline: DesiredState,
    *,
    source_git_sha: str | None = None,
    baseline_git_sha: str | None = None,
    initial_deployment: bool = False,
) -> ImpactPlan:
    _check_stability_preconditions(current, baseline)
    current_data = canonical_state(current)
    baseline_data = canonical_state(baseline)
    changes: list[dict[str, Any]] = []
    affected: dict[str, set[str]] = {key: set() for key in AFFECTED_KEYS}
    destructive = False

    current_nodes = _by_id(current_data["nodes"])
    baseline_nodes = _by_id(baseline_data["nodes"])
    current_instances = _by_id(current_data["instances"])
    baseline_instances = _by_id(baseline_data["instances"])
    current_fleets = _by_id(current_data["fleets"])
    baseline_fleets = _by_id(baseline_data["fleets"])
    current_node_models = {node.object_id: node for node in current.nodes}
    baseline_node_models = {node.object_id: node for node in baseline.nodes}

    for section in sorted(current_data["common"]):
        current_section = current_data["common"][section]
        baseline_section = baseline_data["common"][section]
        if current_section == baseline_section:
            continue
        changes.append(
            {
                "type": "COMMON_CHANGED",
                "section": section,
                "fields": _changed_fields(current_section, baseline_section),
            }
        )
        changed_node_ids = {
            node_id
            for node_id in set(current_nodes) & set(baseline_nodes)
            if current_nodes[node_id]["common"][section] != baseline_nodes[node_id]["common"][section]
        }
        active_instances: set[str] = set()
        _add_instances_for_nodes(active_instances, current_instances, changed_node_ids)
        if section in {"components", "limits", "xray"}:
            affected["configure"].update(active_instances)
            affected["node_runtime"].update(active_instances)
        if section == "components":
            affected["monitoring"].update(active_instances)
        if section == "networking":
            affected["configure"].update(active_instances)
            affected["management"].update(active_instances)
            affected["monitoring"].update(active_instances)
            affected["dns_nodes"].update(
                node_id
                for node_id in changed_node_ids
                if current_nodes[node_id]["role"] == "entry"
                and current_nodes[node_id]["common"]["networking"]["dns"]
                != baseline_nodes[node_id]["common"]["networking"]["dns"]
            )
        if section == "observability":
            affected["monitoring"].update(active_instances)
        if section == "limits" and "degradation_threshold_percent" in _changed_fields(
            current_section,
            baseline_section,
        ):
            affected["monitoring"].update(active_instances)

    if current_data["environment"] != baseline_data["environment"]:
        fields = _changed_fields(current_data["environment"], baseline_data["environment"])
        changes.append({"type": "ENVIRONMENT_CHANGED", "environment": current.environment.object_id, "fields": fields})
        if "control" in fields:
            affected["control"].add(current.environment.object_id)
        if set(fields) - {"control"}:
            affected["configure"].update(current_instances)
            affected["node_runtime"].update(current_instances)
        if "dns_zone" in fields:
            affected["dns_nodes"].update(_node_ids_with_role(current_nodes, "entry"))
        if "management_network" in fields:
            affected["monitoring"].update(_active_instance_ids(current_instances))

    for fleet_id in sorted(set(current_fleets) - set(baseline_fleets)):
        fleet = current_fleets[fleet_id]
        changes.append({"type": "FLEET_ADDED", "fleet_id": fleet_id})
        affected["backend_nodes"].update((*fleet["entries"], *fleet["exits"]))
        _add_instances_for_nodes(affected["node_runtime"], current_instances, (*fleet["entries"], *fleet["exits"]))
        _add_instances_for_nodes(affected["monitoring"], current_instances, (*fleet["entries"], *fleet["exits"]))

    for fleet_id in sorted(set(current_fleets) & set(baseline_fleets)):
        current_fleet = current_fleets[fleet_id]
        baseline_fleet = baseline_fleets[fleet_id]
        current_members = set((*current_fleet["entries"], *current_fleet["exits"]))
        baseline_members = set((*baseline_fleet["entries"], *baseline_fleet["exits"]))
        if current_fleet["entries"] != baseline_fleet["entries"] or current_fleet["exits"] != baseline_fleet["exits"]:
            added = sorted(current_members - baseline_members)
            removed = sorted(baseline_members - current_members)
            changes.append(
                {
                    "type": "FLEET_MEMBERSHIP_CHANGED",
                    "fleet_id": fleet_id,
                    "added_node_ids": added,
                    "removed_node_ids": removed,
                }
            )
            destructive = destructive or bool(removed)
            affected["backend_nodes"].update(current_members | baseline_members)
            _add_instances_for_nodes(affected["node_runtime"], current_instances, current_members)
            _add_instances_for_nodes(affected["monitoring"], current_instances, current_members)
            _add_instances_for_nodes(affected["monitoring"], baseline_instances, baseline_members)

        current_bridges = {item["routing_key"]: item for item in current_fleet["bridges"]}
        baseline_bridges = {item["routing_key"]: item for item in baseline_fleet["bridges"]}
        for routing_key in sorted(set(current_bridges) - set(baseline_bridges)):
            bridge = current_bridges[routing_key]
            changes.append({"type": "BRIDGE_ADDED", "fleet_id": fleet_id, **bridge})
            affected["backend_nodes"].add(bridge["entry"])
            _add_instances_for_nodes(affected["node_runtime"], current_instances, (bridge["entry"],))
        for routing_key in sorted(set(baseline_bridges) - set(current_bridges)):
            bridge = baseline_bridges[routing_key]
            changes.append({"type": "BRIDGE_REMOVED", "fleet_id": fleet_id, **bridge})
            destructive = True
            affected["backend_nodes"].add(bridge["entry"])
            _add_instances_for_nodes(affected["node_runtime"], current_instances, (bridge["entry"],))
        for routing_key in sorted(set(current_bridges) & set(baseline_bridges)):
            current_bridge = current_bridges[routing_key]
            baseline_bridge = baseline_bridges[routing_key]
            if current_bridge != baseline_bridge:
                changes.append(
                    {
                        "type": "BRIDGE_CHANGED",
                        "fleet_id": fleet_id,
                        "routing_key": routing_key,
                        "fields": _changed_fields(current_bridge, baseline_bridge, exclude=("routing_key",)),
                    }
                )
                affected["backend_nodes"].add(current_bridge["entry"])
                _add_instances_for_nodes(affected["node_runtime"], current_instances, (current_bridge["entry"],))

    for node_id in sorted(set(current_nodes) - set(baseline_nodes)):
        changes.append({"type": "LOGICAL_NODE_ADDED", "node_id": node_id})
        affected["backend_nodes"].add(node_id)
        if current_nodes[node_id]["role"] == "entry":
            affected["dns_nodes"].add(node_id)
        _add_instances_for_nodes(affected["node_runtime"], current_instances, (node_id,))
        _add_instances_for_nodes(affected["monitoring"], current_instances, (node_id,))
    for node_id in sorted(set(baseline_nodes) - set(current_nodes)):
        changes.append({"type": "LOGICAL_NODE_REMOVED", "node_id": node_id})
        destructive = True
        affected["backend_nodes"].add(node_id)
        if baseline_nodes[node_id]["role"] == "entry":
            affected["dns_nodes"].add(node_id)
        _add_instances_for_nodes(affected["node_runtime"], baseline_instances, (node_id,))
        _add_instances_for_nodes(affected["monitoring"], baseline_instances, (node_id,))
    for node_id in sorted(set(current_nodes) & set(baseline_nodes)):
        current_node = current_nodes[node_id]
        baseline_node = baseline_nodes[node_id]
        if current_node != baseline_node:
            fields = _changed_fields(current_node, baseline_node, exclude=("id", "common"))
            common_changed = (
                current_node["common"] != baseline_node["common"]
                and current_node_models[node_id].common_overrides
                != baseline_node_models[node_id].common_overrides
            )
            if common_changed:
                fields.append("common")
                fields.sort()
            if not fields:
                continue
            changes.append({"type": "LOGICAL_NODE_CHANGED", "node_id": node_id, "fields": fields})
            dns_common_changed = (
                common_changed
                and current_node["common"]["networking"]["dns"]
                != baseline_node["common"]["networking"]["dns"]
            )
            object_fields = set(fields) - {"common"}
            if object_fields:
                affected["backend_nodes"].add(node_id)
                if current_node["role"] == "entry" and "hostname" in fields:
                    affected["dns_nodes"].add(node_id)
                _add_instances_for_nodes(affected["configure"], current_instances, (node_id,))
                _add_instances_for_nodes(affected["node_runtime"], current_instances, (node_id,))
                _add_instances_for_nodes(affected["monitoring"], current_instances, (node_id,))
                if current_node["role"] == "exit":
                    _add_linked_entries(current, baseline, node_id, affected["node_runtime"], affected["configure"])
            if common_changed:
                _add_node_common_impacts(
                    current_node,
                    baseline_node,
                    current_instances,
                    affected,
                )
            if current_node["role"] == "entry" and dns_common_changed:
                affected["dns_nodes"].add(node_id)

    baseline_serving = _serving_by_node(baseline_instances)
    current_serving = _serving_by_node(current_instances)
    replaced_old: set[str] = set()
    replaced_new: set[str] = set()
    for node_id in sorted(set(baseline_serving) & set(current_serving)):
        old_id = baseline_serving[node_id]
        new_id = current_serving[node_id]
        if old_id != new_id:
            replaced_old.add(old_id)
            replaced_new.add(new_id)
            changes.append(
                {
                    "type": "INSTANCE_REPLACED",
                    "logical_node_id": node_id,
                    "from_instance_id": old_id,
                    "to_instance_id": new_id,
                }
            )
            affected["provision"].add(new_id)
            affected["configure"].add(new_id)
            affected["backend_nodes"].add(node_id)
            if current_nodes[node_id]["role"] == "entry":
                affected["dns_nodes"].add(node_id)
            affected["monitoring"].update((old_id, new_id))
            affected["management"].update((old_id, new_id))
            if old_id in current_instances:
                affected["drain"].add(old_id)
            else:
                affected["retire"].add(old_id)
            affected["node_runtime"].add(new_id)
            if current_nodes[node_id]["role"] == "exit":
                _add_linked_entries(current, baseline, node_id, affected["node_runtime"], affected["configure"])

    for instance_id in sorted(set(current_instances) - set(baseline_instances) - replaced_new):
        instance = current_instances[instance_id]
        changes.append({"type": "INSTANCE_ADDED", "instance_id": instance_id, "logical_node_id": instance["logical_node"]})
        affected["provision"].add(instance_id)
        affected["configure"].add(instance_id)
        affected["monitoring"].add(instance_id)
        affected["management"].add(instance_id)
        node_id = instance["logical_node"]
        if instance["target_state"] == "serving" and current_nodes[node_id]["role"] == "entry":
            affected["dns_nodes"].add(node_id)
    for instance_id in sorted(set(baseline_instances) - set(current_instances) - replaced_old):
        instance = baseline_instances[instance_id]
        changes.append({"type": "INSTANCE_REMOVED", "instance_id": instance_id, "logical_node_id": instance["logical_node"]})
        affected["retire"].add(instance_id)
        affected["monitoring"].add(instance_id)
        affected["management"].add(instance_id)
        node_id = instance["logical_node"]
        if instance["target_state"] == "serving" and baseline_nodes[node_id]["role"] == "entry":
            affected["dns_nodes"].add(node_id)
    for instance_id in sorted(set(current_instances) & set(baseline_instances)):
        current_instance = current_instances[instance_id]
        baseline_instance = baseline_instances[instance_id]
        if current_instance != baseline_instance:
            fields = _changed_fields(current_instance, baseline_instance, exclude=("id",))
            changes.append({"type": "INSTANCE_CHANGED", "instance_id": instance_id, "fields": fields})
            affected["configure"].add(instance_id)
            affected["monitoring"].add(instance_id)
            if "target_state" in fields and current_instance["target_state"] == "draining":
                affected["drain"].add(instance_id)
            node_id = current_instance["logical_node"]
            baseline_was_serving = baseline_instance["target_state"] == "serving"
            current_is_serving = current_instance["target_state"] == "serving"
            if (
                current_nodes[node_id]["role"] == "entry"
                and (current_is_serving or baseline_was_serving)
                and ({"public_address", "target_state"} & set(fields))
            ):
                affected["dns_nodes"].add(node_id)
            if "public_address" in fields:
                if current_nodes[node_id]["role"] == "exit":
                    _add_linked_entries(current, baseline, node_id, affected["node_runtime"], affected["configure"])

    changes.sort(key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))
    return ImpactPlan(
        environment=current.environment.object_id,
        source_git_sha=source_git_sha,
        baseline_git_sha=baseline_git_sha,
        initial_deployment=initial_deployment,
        source_digest=canonical_digest(current),
        baseline_digest=canonical_digest(baseline),
        changes=tuple(changes),
        affected={key: tuple(sorted(values)) for key, values in affected.items()},
        destructive=destructive,
    )


def build_initial_baseline(current: DesiredState) -> DesiredState:
    """Return an empty, environment-compatible state for an explicit first deploy."""

    return DesiredState(
        common=current.common,
        environment_common=current.environment_common,
        node_common={},
        environment=replace(current.environment, control=None),
        fleets=(),
        nodes=(),
        instances=(),
        fleet_ids={},
    )


def _check_stability_preconditions(current: DesiredState, baseline: DesiredState) -> None:
    if current.environment.object_id != baseline.environment.object_id:
        raise PlanningError("source and baseline environments differ")
    for fleet_id, fleet_number in baseline.fleet_ids.items():
        if current.fleet_ids.get(fleet_id) != fleet_number:
            raise PlanningError(f"append-only fleet ID mapping changed or disappeared for {fleet_id!r}")

    current_fleets = {fleet.object_id: fleet for fleet in current.fleets}
    for baseline_fleet in baseline.fleets:
        current_fleet = current_fleets.get(baseline_fleet.object_id)
        if current_fleet is None:
            raise PlanningError(f"previously accepted fleet {baseline_fleet.object_id!r} disappeared")
        current_bridges = {bridge.routing_key: bridge for bridge in current_fleet.bridges}
        for bridge in baseline_fleet.bridges:
            current_bridge = current_bridges.get(bridge.routing_key)
            if current_bridge is not None and (current_bridge.entry, current_bridge.exit) != (bridge.entry, bridge.exit):
                raise PlanningError(f"routing_key {bridge.routing_key!r} was rebound to a different pair")

    current_nodes = {node.object_id: node for node in current.nodes}
    for node in baseline.nodes:
        current_node = current_nodes.get(node.object_id)
        if current_node is not None and current_node.role != node.role:
            raise PlanningError(f"logical node {node.object_id!r} changed role")
    current_instances = {instance.object_id: instance for instance in current.instances}
    for instance in baseline.instances:
        current_instance = current_instances.get(instance.object_id)
        if current_instance is not None and current_instance.logical_node != instance.logical_node:
            raise PlanningError(f"instance {instance.object_id!r} was rebound to a different logical node")


def _by_id(items: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in items}


def _active_instance_ids(instances: dict[str, dict[str, Any]]) -> set[str]:
    return {
        instance_id
        for instance_id, instance in instances.items()
        if instance["target_state"] != "retired"
    }


def _node_ids_with_role(nodes: dict[str, dict[str, Any]], role: str) -> set[str]:
    return {node_id for node_id, node in nodes.items() if node["role"] == role}


def _changed_fields(current: dict[str, Any], baseline: dict[str, Any], *, exclude: tuple[str, ...] = ()) -> list[str]:
    keys = (set(current) | set(baseline)) - set(exclude)
    return sorted(key for key in keys if current.get(key) != baseline.get(key))


def _serving_by_node(instances: dict[str, dict[str, Any]]) -> dict[str, str]:
    return {
        instance["logical_node"]: instance_id
        for instance_id, instance in instances.items()
        if instance["target_state"] == "serving"
    }


def _add_instances_for_nodes(target: set[str], instances: dict[str, dict[str, Any]], node_ids: Iterable[str]) -> None:
    wanted = set(node_ids)
    target.update(
        instance_id
        for instance_id, instance in instances.items()
        if instance["logical_node"] in wanted and instance["target_state"] != "retired"
    )


def _add_node_common_impacts(
    current_node: dict[str, Any],
    baseline_node: dict[str, Any],
    current_instances: dict[str, dict[str, Any]],
    affected: dict[str, set[str]],
) -> None:
    node_id = current_node["id"]
    current_common = current_node["common"]
    baseline_common = baseline_node["common"]
    sections = {
        section
        for section in current_common
        if current_common[section] != baseline_common[section]
    }
    instance_ids: set[str] = set()
    _add_instances_for_nodes(instance_ids, current_instances, (node_id,))
    if sections & {"components", "limits", "xray"}:
        affected["configure"].update(instance_ids)
        affected["node_runtime"].update(instance_ids)
    if "components" in sections:
        affected["monitoring"].update(instance_ids)
    if "networking" in sections:
        affected["configure"].update(instance_ids)
        affected["management"].update(instance_ids)
        affected["monitoring"].update(instance_ids)
    if "observability" in sections:
        affected["monitoring"].update(instance_ids)
    if (
        "limits" in sections
        and current_common["limits"]["degradation_threshold_percent"]
        != baseline_common["limits"]["degradation_threshold_percent"]
    ):
        affected["monitoring"].update(instance_ids)


def _linked_entry_nodes(state: DesiredState, exit_node_id: str) -> set[str]:
    return {
        bridge.entry
        for fleet in state.fleets
        for bridge in fleet.bridges
        if bridge.exit == exit_node_id
    }


def _add_linked_entries(
    current: DesiredState,
    baseline: DesiredState,
    exit_node_id: str,
    runtime_target: set[str],
    configure_target: set[str],
) -> None:
    entry_nodes = _linked_entry_nodes(current, exit_node_id) | _linked_entry_nodes(baseline, exit_node_id)
    current_instances = _by_id(canonical_state(current)["instances"])
    _add_instances_for_nodes(runtime_target, current_instances, entry_nodes)
    _add_instances_for_nodes(configure_target, current_instances, entry_nodes)
