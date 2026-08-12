"""Provider-neutral provisioning boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from fleetctl.model import Instance


@dataclass(frozen=True, slots=True)
class InstanceDescription:
    instance_id: str
    provider_name: str
    provider_resource_id: str
    public_address: str
    evidence: str


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    name: str
    passed: bool
    diagnostic: str

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "passed": self.passed,
            "diagnostic": self.diagnostic,
        }


@dataclass(frozen=True, slots=True)
class ProvisioningReport:
    instance_id: str
    provider_name: str
    checks: tuple[PreflightCheck, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def to_dict(self) -> dict[str, object]:
        return {
            "instance_id": self.instance_id,
            "provider_name": self.provider_name,
            "passed": self.passed,
            "checks": [check.to_dict() for check in self.checks],
        }


@dataclass(frozen=True, slots=True)
class OperatorActionRequired:
    action: str
    instance_id: str
    diagnostic: str

    def to_dict(self) -> dict[str, str]:
        return {
            "status": "OPERATOR_ACTION_REQUIRED",
            "action": self.action,
            "instance_id": self.instance_id,
            "diagnostic": self.diagnostic,
        }


class ProvisioningAdapter(Protocol):
    def describe(self, instance: Instance) -> InstanceDescription: ...

    def preflight(self, instance: Instance) -> ProvisioningReport: ...

    def create(self, instance: Instance) -> OperatorActionRequired: ...

    def destroy(self, instance: Instance) -> OperatorActionRequired: ...
