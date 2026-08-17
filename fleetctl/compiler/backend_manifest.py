"""Deterministic full-snapshot projection for ApplyFleetManifest."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fleetctl.model import DesiredState, Fleet, Instance, LogicalNode
from fleetctl.planning.canonical import canonical_digest
from fleetctl.planning.model import ImpactPlan

from .addressing import agent_certificate_identity, agent_endpoint, agent_tls_server_name


MANIFEST_SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_UINT64 = 2**64 - 1


class BackendManifestError(ValueError):
    """The manifest cannot be safely built for the supplied impact plan."""


def compile_backend_manifest(
    state: DesiredState,
    plan: ImpactPlan,
    *,
    revision: int,
    allow_destructive: bool,
) -> dict[str, Any]:
    """Build one complete, deterministic ApplyFleetManifest request projection."""

    _validate_envelope(state, plan, revision, allow_destructive)
    serving = _serving_instances(state)
    request: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "revision": revision,
        "allow_destructive": allow_destructive,
        "nodes": [
            _compile_node(state, node, serving[node.object_id])
            for node in sorted(state.nodes, key=lambda item: item.object_id)
        ],
        "fleets": [
            _compile_fleet(state, fleet)
            for fleet in sorted(state.fleets, key=lambda item: item.object_id)
        ],
    }
    encoded = _compact_json_bytes(request)
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise BackendManifestError(
            f"backend manifest is {len(encoded)} bytes, exceeding the {MAX_MANIFEST_BYTES}-byte client limit"
        )
    return request


def backend_manifest_bytes(request: dict[str, Any]) -> bytes:
    """Serialize the request as a stable, reviewable JSON artifact."""

    return (json.dumps(request, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def backend_manifest_payload_digest(request: dict[str, Any]) -> str:
    """Hash the local desired payload, excluding revision-scoped request fields."""

    payload = {
        "schema_version": request["schema_version"],
        "nodes": request["nodes"],
        "fleets": request["fleets"],
    }
    return f"sha256:{hashlib.sha256(_compact_json_bytes(payload)).hexdigest()}"


def _validate_envelope(
    state: DesiredState,
    plan: ImpactPlan,
    revision: int,
    allow_destructive: bool,
) -> None:
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or not 1 <= revision <= MAX_UINT64
    ):
        raise BackendManifestError(
            "manifest revision must be an integer in the uint64 range 1..2^64-1"
        )
    if not isinstance(allow_destructive, bool):
        raise BackendManifestError("allow_destructive must be an explicit boolean")
    if plan.environment != state.environment.object_id:
        raise BackendManifestError("impact plan belongs to another environment")
    if plan.source_digest != canonical_digest(state):
        raise BackendManifestError("impact plan does not describe the desired state being rendered")
    if plan.destructive and not allow_destructive:
        raise BackendManifestError("destructive impact plan requires explicit allow_destructive=true")
    if allow_destructive and not plan.destructive:
        raise BackendManifestError("allow_destructive=true refused because the impact plan is non-destructive")


def _serving_instances(state: DesiredState) -> dict[str, Instance]:
    serving: dict[str, Instance] = {}
    for instance in state.instances:
        if instance.target_state != "serving":
            continue
        if instance.logical_node in serving:
            raise BackendManifestError(
                f"logical node {instance.logical_node!r} has multiple serving instances"
            )
        serving[instance.logical_node] = instance
    missing = sorted(node.object_id for node in state.nodes if node.object_id not in serving)
    if missing:
        raise BackendManifestError(f"logical nodes without a serving instance: {', '.join(missing)}")
    return serving


def _compile_node(
    state: DesiredState,
    node: LogicalNode,
    instance: Instance,
) -> dict[str, Any]:
    common = state.common_for_node(node.object_id)
    return {
        "node_id": node.object_id,
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
        "public": {
            "address": node.hostname,
            "port": node.public_port,
            "reality_public_key": node.reality_public_key,
            "server_name": node.server_name,
            "short_id": node.reality_short_id,
            "fingerprint": node.fingerprint,
            "flow": node.flow,
            "transport": node.transport,
        },
        "display_name": node.display_name,
    }


def _compile_fleet(state: DesiredState, fleet: Fleet) -> dict[str, Any]:
    fleet_number = state.fleet_ids.get(fleet.object_id)
    if fleet_number is None:
        raise BackendManifestError(f"fleet {fleet.object_id!r} has no vpn_fleet_id")
    return {
        "vpn_fleet_id": fleet_number,
        "node_ids": sorted({*fleet.entries, *fleet.exits}),
        "bridges": [
            {
                "routing_key": bridge.routing_key,
                "entry_node_id": bridge.entry,
                "exit_node_id": bridge.exit,
                "egress_tag": (
                    f"{state.common_for_node(bridge.entry).xray.exit_outbound_prefix}"
                    f"{bridge.exit}"
                ),
                "display_name": bridge.display_name,
            }
            for bridge in sorted(fleet.bridges, key=lambda item: item.routing_key)
        ],
    }


def _compact_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
