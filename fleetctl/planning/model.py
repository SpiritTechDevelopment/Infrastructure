"""Stable impact-plan representation."""

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
            "rollout": {"destructive": self.destructive},
        }

    def to_json_bytes(self) -> bytes:
        return (json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


class PlanningError(Exception):
    pass
