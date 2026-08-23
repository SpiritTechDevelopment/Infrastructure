"""Manual, provider-neutral provisioning implementation."""

from __future__ import annotations

import ipaddress

from fleetctl.model import Instance

from .model import (
    InstanceDescription,
    PreflightCheck,
    ProvisioningReport,
)


PLACEHOLDER_MARKERS = ("placeholder", "replace", "example", "<", ">")


class ManualProvisioningAdapter:
    def describe(self, instance: Instance) -> InstanceDescription:
        return InstanceDescription(
            instance_id=instance.object_id,
            provider_name=instance.provider_name,
            provider_resource_id=instance.provider_resource_id,
            public_address=instance.public_address,
            evidence="desired_state_operator_declaration",
        )

    def preflight(self, instance: Instance) -> ProvisioningReport:
        description = self.describe(instance)
        provider_is_manual = description.provider_name == "manual"
        resource_present = _is_real_value(description.provider_resource_id)
        try:
            address = ipaddress.ip_address(description.public_address)
            address_valid = address.is_global
            address_diagnostic = (
                f"public address {address} is globally routable"
                if address_valid
                else f"public address {address} is reserved, private, or otherwise not globally routable"
            )
        except ValueError:
            address_valid = False
            address_diagnostic = f"public address is not a valid IP address: {description.public_address!r}"

        return ProvisioningReport(
            instance_id=description.instance_id,
            provider_name=description.provider_name,
            checks=(
                PreflightCheck(
                    name="manual_provider",
                    passed=provider_is_manual,
                    diagnostic=(
                        "manual provider selected"
                        if provider_is_manual
                        else f"no provisioning adapter is available for provider {description.provider_name!r}"
                    ),
                ),
                PreflightCheck(
                    name="provider_resource_id",
                    passed=resource_present,
                    diagnostic=(
                        "operator supplied a non-placeholder provider resource ID"
                        if resource_present
                        else "provider_resource_id is missing or still contains a placeholder"
                    ),
                ),
                PreflightCheck(
                    name="public_address",
                    passed=address_valid,
                    diagnostic=address_diagnostic,
                ),
            ),
        )


def _is_real_value(value: str) -> bool:
    normalized = value.strip().lower()
    return bool(normalized) and not any(marker in normalized for marker in PLACEHOLDER_MARKERS)
