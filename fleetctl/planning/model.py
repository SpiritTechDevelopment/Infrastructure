"""Стабильное представление impact-плана."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ImpactPlan:
    environment: str
    source_git_sha: str | None
    baseline_git_sha: str | None
    initial_deployment: bool
    source_digest: str
    baseline_digest: str
    changes: tuple[dict[str, Any], ...]
    affected: dict[str, tuple[str, ...]]
    destructive: bool

    @property
    def transition_kinds(self) -> tuple[str, ...]:
        """Stable, operator-facing classification of the semantic diff."""

        change_types = {item.get("type") for item in self.changes}
        kinds: list[str] = []
        if "INSTANCE_REPLACED" in change_types:
            kinds.append("replacement")
        membership_changes = tuple(
            item for item in self.changes if item.get("type") == "FLEET_MEMBERSHIP_CHANGED"
        )
        membership_added = any(item.get("added_node_ids") for item in membership_changes)
        if membership_added or change_types & {
            "LOGICAL_NODE_ADDED",
            "FLEET_ADDED",
            "INSTANCE_ADDED",
            "BRIDGE_ADDED",
        }:
            kinds.append("addition")
        if self.destructive or "INSTANCE_REMOVED" in change_types:
            kinds.append("removal")
        if change_types & {
            "COMMON_CHANGED",
            "ENVIRONMENT_CHANGED",
            "LOGICAL_NODE_CHANGED",
            "BRIDGE_CHANGED",
            "INSTANCE_CHANGED",
            "NODE_RUNTIME_INPUTS_CHANGED",
        } or any(
            not item.get("added_node_ids") and not item.get("removed_node_ids")
            for item in membership_changes
        ):
            kinds.append("modification")
        return tuple(kinds or ("no-op",))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "environment": self.environment,
            "source_git_sha": self.source_git_sha,
            "baseline_git_sha": self.baseline_git_sha,
            "initial_deployment": self.initial_deployment,
            "source_digest": self.source_digest,
            "baseline_digest": self.baseline_digest,
            "changes": list(self.changes),
            "affected": {key: list(value) for key, value in sorted(self.affected.items())},
            "rollout": {
                "destructive": self.destructive,
                "transition_kinds": list(self.transition_kinds),
            },
        }

    def to_json_bytes(self) -> bytes:
        return (json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


class PlanningError(Exception):
    pass
