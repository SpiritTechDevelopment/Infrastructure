"""Transport-neutral structured readiness results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class GateSpec:
    name: str
    instance_id: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    passed: bool
    diagnostic: str


class ReadinessProbe(Protocol):
    def execute(self, gate: GateSpec, *, timeout_seconds: int) -> ProbeOutcome: ...


@dataclass(frozen=True, slots=True)
class GateResult:
    name: str
    instance_id: str
    passed: bool
    diagnostic: str
    timestamp: str

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "instance_id": self.instance_id,
            "passed": self.passed,
            "diagnostic": self.diagnostic,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True, slots=True)
class GateReport:
    environment: str
    instance_id: str
    results: tuple[GateResult, ...]

    @property
    def passed(self) -> bool:
        return bool(self.results) and all(result.passed for result in self.results)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "environment": self.environment,
            "instance_id": self.instance_id,
            "passed": self.passed,
            "results": [result.to_dict() for result in self.results],
        }
