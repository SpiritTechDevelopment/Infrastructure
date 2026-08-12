"""Read-only validation of generated platform artifacts."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .output import MARKER


class PlatformArtifactsError(Exception):
    pass


def validate_platform_artifacts(build_directory: Path, environment: str) -> str:
    directory = build_directory.resolve()
    if not (directory / MARKER).is_file():
        raise PlatformArtifactsError(f"not a fleetctl-managed build directory: {directory}")
    plan = _read_mapping(directory / "platform-plan.json")
    if plan.get("_notice") != "GENERATED — DO NOT EDIT" or plan.get("schema_version") != 1:
        raise PlatformArtifactsError("platform plan has an unsupported schema or no generated marker")
    if plan.get("environment") != environment:
        raise PlatformArtifactsError("platform plan belongs to another environment")
    platform = _mapping(plan, "platform", "platform plan")
    host = platform.get("id")
    if not isinstance(host, str) or host != f"{environment}-platform":
        raise PlatformArtifactsError("platform plan has an invalid platform ID")
    vault = _mapping(plan, "vault", "platform plan")
    if not isinstance(vault.get("image"), str) or re.fullmatch(r".+@sha256:[0-9a-f]{64}", vault["image"]) is None:
        raise PlatformArtifactsError("platform Vault image must use an immutable sha256 digest")
    api = _mapping(vault, "api", "platform plan Vault")
    if api.get("bind_address") != "127.0.0.1":
        raise PlatformArtifactsError("Vault API must remain loopback-only for the GitHub-hosted runner tunnel")
    _validate_inventory(
        directory / "platform-bootstrap-inventory.json", plan, host, "spiritvpn_platform_bootstrap", "root", "bootstrap"
    )
    _validate_inventory(
        directory / "platform-inventory.json", plan, host, "spiritvpn_platform", "deploy", "runtime"
    )
    return host


def validate_platform_known_hosts(build_directory: Path, known_hosts_path: Path) -> str:
    directory = build_directory.resolve()
    if not (directory / MARKER).is_file():
        raise PlatformArtifactsError(f"not a fleetctl-managed build directory: {directory}")
    plan = _read_mapping(directory / "platform-plan.json")
    if plan.get("_notice") != "GENERATED — DO NOT EDIT" or plan.get("schema_version") != 1:
        raise PlatformArtifactsError("platform plan has an unsupported schema or no generated marker")
    platform = _mapping(plan, "platform", "platform plan")
    ssh = _mapping(platform, "ssh", "platform plan platform")
    expected = ssh.get("host_key_fingerprints")
    if not isinstance(expected, list) or not expected or not all(isinstance(item, str) for item in expected):
        raise PlatformArtifactsError("platform plan has no SSH host-key fingerprints")
    try:
        lines = known_hosts_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise PlatformArtifactsError(f"cannot read pinned known_hosts: {exc}") from exc
    actual: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if fields[0].startswith("@"):
            fields = fields[1:]
        if len(fields) < 3:
            raise PlatformArtifactsError("pinned known_hosts contains an invalid line")
        try:
            key = base64.b64decode(fields[2], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise PlatformArtifactsError("pinned known_hosts contains invalid key data") from exc
        digest = base64.b64encode(hashlib.sha256(key).digest()).decode("ascii").rstrip("=")
        actual.add(f"SHA256:{digest}")
    matched = sorted(set(expected) & actual)
    if not matched:
        raise PlatformArtifactsError("pinned known_hosts does not match any reviewed platform fingerprint")
    return matched[0]


def _validate_inventory(
    path: Path,
    plan: dict[str, Any],
    host: str,
    group: str,
    user: str,
    phase: str,
) -> None:
    inventory = _read_mapping(path)
    all_group = _mapping(inventory, "all", path.name)
    children = _mapping(all_group, "children", f"{path.name}.all")
    group_value = _mapping(children, group, f"{path.name}.children")
    hosts = _mapping(group_value, "hosts", f"{path.name}.{group}")
    if set(hosts) != {host}:
        raise PlatformArtifactsError(f"{path.name} must contain exactly the compiled platform host")
    hostvars = hosts[host]
    if not isinstance(hostvars, dict):
        raise PlatformArtifactsError(f"{path.name} host variables must be an object")
    expected = {"ansible_host", "ansible_user", "spiritvpn_connection_phase", "spiritvpn_platform_plan_file"}
    if set(hostvars) != expected:
        raise PlatformArtifactsError(f"{path.name} contains unexpected host variables")
    platform = _mapping(plan, "platform", "platform plan")
    if (
        hostvars["ansible_host"] != platform.get("public_address")
        or hostvars["ansible_user"] != user
        or hostvars["spiritvpn_connection_phase"] != phase
        or hostvars["spiritvpn_platform_plan_file"] != "platform-plan.json"
    ):
        raise PlatformArtifactsError(f"{path.name} disagrees with the platform plan")


def _read_mapping(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PlatformArtifactsError(f"cannot read JSON artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PlatformArtifactsError(f"{path} must contain a JSON object")
    return value


def _mapping(value: dict[str, Any], key: str, context: str) -> dict[str, Any]:
    item = value.get(key)
    if not isinstance(item, dict):
        raise PlatformArtifactsError(f"{context}.{key} must be an object")
    return item
