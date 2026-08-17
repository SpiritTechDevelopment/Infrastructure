"""Deterministic serialization of all currently supported projections."""

from __future__ import annotations

import json

from fleetctl.model import DesiredState

from .bootstrap import compile_bootstrap_inventory
from .control import compile_control_plan
from .dns import compile_dns_plan
from .inventory import compile_ansible_inventory
from .monitoring import compile_monitoring_targets
from .node_plans import compile_node_plans


def render_files(state: DesiredState) -> dict[str, bytes]:
    files = {
        "ansible-inventory.json": _json_bytes(compile_ansible_inventory(state)),
        "bootstrap-inventory.json": _json_bytes(compile_bootstrap_inventory(state)),
        "dns-plan.json": _json_bytes(compile_dns_plan(state)),
        "monitoring-targets.json": _json_bytes(compile_monitoring_targets(state)),
    }
    control_plan = compile_control_plan(state)
    if control_plan is not None:
        files["control-plan.json"] = _json_bytes(control_plan)
    for instance_id, plan in compile_node_plans(state).items():
        files[f"node-plans/{instance_id}.json"] = _json_bytes(plan)
    return dict(sorted(files.items()))


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
