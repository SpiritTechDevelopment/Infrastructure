"""Public validation use case."""

from __future__ import annotations

from pathlib import Path

from fleetctl.model import (
    CommonOverrideError,
    DesiredState,
    Environment,
    Fleet,
    Instance,
    LogicalNode,
    Platform,
    apply_common_overrides,
)

from .issues import DesiredStateInvalid, ValidationIssue
from .loader import load_common_config, load_environment_objects, load_fleet_ids, load_schemas
from .semantic import validate_semantics


def validate_environment(
    repo_root: Path,
    environment_name: str,
    *,
    desired_root: Path | None = None,
) -> DesiredState:
    repo_root = repo_root.resolve()
    desired_root = (desired_root or repo_root / "desired").resolve()
    schema_root = repo_root / "contracts" / "desired-state"
    validators, issues = load_schemas(schema_root)
    common, common_issues = load_common_config(desired_root / "common", schema_root)
    objects, object_issues = load_environment_objects(
        desired_root / "environments" / environment_name,
        validators,
    )
    fleet_ids, fleet_id_issues = load_fleet_ids(desired_root / "fleet-ids.yml")
    issues.extend(object_issues)
    issues.extend(fleet_id_issues)
    issues.extend(common_issues)

    environments = [item for item in objects if isinstance(item, Environment)]
    fleets = tuple(item for item in objects if isinstance(item, Fleet))
    nodes = tuple(item for item in objects if isinstance(item, LogicalNode))
    instances = tuple(item for item in objects if isinstance(item, Instance))
    platforms = tuple(item for item in objects if isinstance(item, Platform))
    if len(environments) != 1:
        issues.append(
            ValidationIssue.at(
                desired_root / "environments" / environment_name,
                "ENVIRONMENT_COUNT",
                f"expected exactly one Environment object, found {len(environments)}",
            )
        )
    elif environments[0].object_id != environment_name:
        issues.append(
            ValidationIssue.at(
                environments[0].source,
                "ENVIRONMENT_ID",
                f"Environment ID must match directory name {environment_name!r}",
            )
        )
    if len(platforms) > 1:
        issues.append(
            ValidationIssue.at(
                desired_root / "environments" / environment_name / "platform",
                "PLATFORM_COUNT",
                f"expected at most one Platform object, found {len(platforms)}",
            )
        )

    if not issues and environments and common is not None:
        environment = environments[0]
        try:
            environment_common = apply_common_overrides(common, environment.common_overrides, environment.source)
        except CommonOverrideError as exc:
            issues.append(ValidationIssue.at(environment.source, "COMMON_OVERRIDE", str(exc)))
            environment_common = common

        node_common = {}
        for node in nodes:
            try:
                node_common[node.object_id] = apply_common_overrides(
                    environment_common,
                    node.common_overrides,
                    node.source,
                )
            except CommonOverrideError as exc:
                issues.append(ValidationIssue.at(node.source, "COMMON_OVERRIDE", str(exc)))
        if issues:
            raise DesiredStateInvalid(issues)
        state = DesiredState(
            common=common,
            environment_common=environment_common,
            node_common=node_common,
            environment=environment,
            platform=platforms[0] if platforms else None,
            fleets=fleets,
            nodes=nodes,
            instances=instances,
            fleet_ids=fleet_ids,
        )
        issues.extend(validate_semantics(state))
    if issues:
        raise DesiredStateInvalid(issues)
    return state
