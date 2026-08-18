"""Read-only validation for the generated Ansible input boundary."""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from .output import MARKER


NODE_PLAN_PATTERN = re.compile(r"^node-plans/[a-z0-9-]+\.json$")


class CompiledArtifactsError(Exception):
    pass


def validate_ansible_artifacts(build_directory: Path, environment: str) -> int:
    build_directory = build_directory.resolve()
    if not (build_directory / MARKER).is_file():
        raise CompiledArtifactsError(f"not a fleetctl-managed build directory: {build_directory}")
    inventory = _read_json(build_directory / "ansible-inventory.json")
    plan_files = _validate_configure_inventory(inventory, build_directory, environment)
    bootstrap = _read_json(build_directory / "bootstrap-inventory.json")
    bootstrap_plan_files = _validate_bootstrap_inventory(bootstrap, build_directory, environment)
    if bootstrap_plan_files != plan_files:
        raise CompiledArtifactsError("bootstrap and configure inventories do not reference the same node plans")

    actual_plan_files = {
        path.resolve()
        for path in (build_directory / "node-plans").glob("*.json")
        if path.is_file() and not path.is_symlink()
    }
    missing_plan_files = plan_files - actual_plan_files
    if missing_plan_files:
        missing = sorted(str(path.name) for path in missing_plan_files)
        raise CompiledArtifactsError(
            f"inventory/node-plan set mismatch; missing={missing}"
        )
    for retired_plan_path in sorted(actual_plan_files - plan_files):
        retired_plan = _read_json(retired_plan_path)
        instance = _mapping(retired_plan, "instance", f"node plan {retired_plan_path.name}")
        logical_node = _mapping(retired_plan, "logical_node", f"node plan {retired_plan_path.name}")
        host = retired_plan_path.stem
        role = logical_node.get("role")
        if instance.get("target_state") != "retired" or role not in {"entry", "exit"}:
            raise CompiledArtifactsError(
                f"node plan is not referenced by active inventories and is not retired: {retired_plan_path.name}"
            )
        _validate_node_plan(retired_plan, environment=environment, host=host, role=role)
    return len(plan_files)


def _validate_configure_inventory(
    inventory: dict[str, Any],
    build_directory: Path,
    environment: str,
) -> set[Path]:
    all_group = _mapping(inventory, "all", "inventory")
    children = _mapping(all_group, "children", "inventory.all")
    fleet = _mapping(children, "spiritvpn_fleet", "inventory.all.children")
    role_groups = _mapping(fleet, "children", "inventory spiritvpn_fleet")

    seen_hosts: set[str] = set()
    plan_files: set[Path] = set()
    for role in ("entry", "exit"):
        group = _mapping(role_groups, role, "inventory spiritvpn_fleet children")
        hosts = _mapping(group, "hosts", f"inventory group {role}")
        for host, raw_hostvars in sorted(hosts.items()):
            if host in seen_hosts:
                raise CompiledArtifactsError(f"host appears in more than one role group: {host}")
            seen_hosts.add(host)
            hostvars = _expect_mapping(raw_hostvars, f"inventory host {host}")
            if set(hostvars) != {"ansible_host", "ansible_port", "spiritvpn_node_plan_file"}:
                raise CompiledArtifactsError(
                    f"inventory host {host} must contain only connection data and spiritvpn_node_plan_file"
                )
            relative_name = hostvars["spiritvpn_node_plan_file"]
            if not isinstance(relative_name, str) or NODE_PLAN_PATTERN.fullmatch(relative_name) is None:
                raise CompiledArtifactsError(f"inventory host {host} has an invalid node-plan path")
            relative = PurePosixPath(relative_name)
            if relative.is_absolute() or ".." in relative.parts:
                raise CompiledArtifactsError(f"inventory host {host} has an unsafe node-plan path")
            plan_path = build_directory.joinpath(*relative.parts)
            if plan_path.is_symlink() or not plan_path.is_file():
                raise CompiledArtifactsError(f"node plan is missing or unsafe for host {host}: {relative_name}")
            plan = _read_json(plan_path)
            _validate_node_plan(plan, environment=environment, host=host, role=role)
            instance = _mapping(plan, "instance", f"node plan {host}")
            if hostvars["ansible_host"] != instance.get("management_address"):
                raise CompiledArtifactsError(f"inventory ansible_host disagrees with node plan for {host}")
            networking = _mapping(
                _mapping(plan, "infrastructure", f"node plan {host}"),
                "networking",
                f"node plan {host} infrastructure",
            )
            if hostvars["ansible_port"] != _mapping(networking, "ssh", f"node plan {host} networking").get("port"):
                raise CompiledArtifactsError(f"inventory ansible_port disagrees with node plan for {host}")
            plan_files.add(plan_path.resolve())

    return plan_files


def _validate_bootstrap_inventory(
    inventory: dict[str, Any],
    build_directory: Path,
    environment: str,
) -> set[Path]:
    all_group = _mapping(inventory, "all", "bootstrap inventory")
    children = _mapping(all_group, "children", "bootstrap inventory.all")
    bootstrap = _mapping(children, "spiritvpn_bootstrap", "bootstrap inventory children")
    hosts = _mapping(bootstrap, "hosts", "bootstrap inventory group")
    plan_files: set[Path] = set()
    for host, raw_hostvars in sorted(hosts.items()):
        hostvars = _expect_mapping(raw_hostvars, f"bootstrap inventory host {host}")
        expected_keys = {
            "ansible_host",
            "ansible_user",
            "spiritvpn_connection_phase",
            "spiritvpn_node_plan_file",
        }
        if set(hostvars) != expected_keys:
            raise CompiledArtifactsError(f"bootstrap inventory host {host} has unexpected variables")
        if hostvars["ansible_user"] != "root" or hostvars["spiritvpn_connection_phase"] != "bootstrap":
            raise CompiledArtifactsError(f"bootstrap inventory host {host} has an unsafe connection mode")
        relative_name = hostvars["spiritvpn_node_plan_file"]
        if not isinstance(relative_name, str) or NODE_PLAN_PATTERN.fullmatch(relative_name) is None:
            raise CompiledArtifactsError(f"bootstrap inventory host {host} has an invalid node-plan path")
        relative = PurePosixPath(relative_name)
        if relative.is_absolute() or ".." in relative.parts:
            raise CompiledArtifactsError(f"bootstrap inventory host {host} has an unsafe node-plan path")
        plan_path = build_directory.joinpath(*relative.parts)
        if plan_path.is_symlink() or not plan_path.is_file():
            raise CompiledArtifactsError(f"bootstrap node plan is missing or unsafe for host {host}")
        plan = _read_json(plan_path)
        role = _mapping(plan, "logical_node", f"node plan {host}").get("role")
        if role not in {"entry", "exit"}:
            raise CompiledArtifactsError(f"node plan {host} has an unsupported role")
        _validate_node_plan(plan, environment=environment, host=host, role=role)
        instance = _mapping(plan, "instance", f"node plan {host}")
        if hostvars["ansible_host"] != instance.get("public_address"):
            raise CompiledArtifactsError(f"bootstrap ansible_host disagrees with public address for {host}")
        plan_files.add(plan_path.resolve())
    return plan_files


def _validate_node_plan(plan: dict[str, Any], *, environment: str, host: str, role: str) -> None:
    if plan.get("_notice") != "GENERATED — DO NOT EDIT":
        raise CompiledArtifactsError(f"node plan {host} is missing the generated marker")
    if plan.get("schema_version") != 1:
        raise CompiledArtifactsError(f"node plan {host} has unsupported schema_version")
    if plan.get("environment") != environment:
        raise CompiledArtifactsError(f"node plan {host} belongs to another environment")

    infrastructure = _mapping(plan, "infrastructure", f"node plan {host}")
    for section in ("components", "networking", "observability", "rollout", "xray", "limits"):
        _mapping(infrastructure, section, f"node plan {host} infrastructure")
    logical_node = _mapping(plan, "logical_node", f"node plan {host}")
    if logical_node.get("role") != role:
        raise CompiledArtifactsError(f"node plan {host} role disagrees with inventory group")
    _mapping(logical_node, "public", f"node plan {host} logical_node")
    _mapping(logical_node, "reality", f"node plan {host} logical_node")
    instance = _mapping(plan, "instance", f"node plan {host}")
    if instance.get("id") != host:
        raise CompiledArtifactsError(f"node plan instance ID disagrees with inventory host {host}")
    _mapping(instance, "bandwidth", f"node plan {host} instance")
    _mapping(instance, "agent", f"node plan {host} instance")
    _mapping(instance, "provider", f"node plan {host} instance")
    _mapping(plan, "routing", f"node plan {host}")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CompiledArtifactsError(f"cannot read JSON artifact {path}: {exc}") from exc
    return _expect_mapping(value, str(path))


def _mapping(value: dict[str, Any], key: str, context: str) -> dict[str, Any]:
    if key not in value:
        raise CompiledArtifactsError(f"{context} is missing {key!r}")
    return _expect_mapping(value[key], f"{context}.{key}")


def _expect_mapping(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CompiledArtifactsError(f"{context} must be an object")
    return value
