#!/usr/bin/env python3
"""Validate encrypted topology envelopes without possessing a decryption key."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

import yaml


ENVIRONMENT_RE = re.compile(r"^[a-z0-9-]{1,63}$")
AGE_RECIPIENT_RE = re.compile(r"^age1[0-9a-z]+$")
ENC_RE = re.compile(r"^ENC\[AES256_GCM,.+\]$")
COMMON_FILES = (
    "components.yml",
    "limits.yml",
    "networking.yml",
    "observability.yml",
    "rollout.yml",
    "xray.yml",
)


def encrypted_leaves(value: Any, path: str) -> list[str]:
    issues: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            issues.extend(encrypted_leaves(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            issues.extend(encrypted_leaves(child, f"{path}[{index}]"))
    # SOPS deliberately preserves null because it carries no scalar value to
    # disclose; schemas use null to mean "inherit the runtime default".
    elif value is not None and (
        not isinstance(value, str) or not ENC_RE.fullmatch(value)
    ):
        issues.append(f"{path}: plaintext scalar in encrypted spec")
    return issues


def check_sops_metadata(path: Path, document: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    sops = document.get("sops")
    if not isinstance(sops, dict):
        return [f"{path}: SOPS metadata is missing"]
    if not ENC_RE.fullmatch(str(sops.get("mac", ""))):
        issues.append(f"{path}: encrypted SOPS MAC is missing")
    recipients = sops.get("age")
    if not isinstance(recipients, list) or len(recipients) < 3:
        issues.append(f"{path}: three age recipient stanzas are required")
    else:
        for recipient in recipients:
            if not isinstance(recipient, dict) or not AGE_RECIPIENT_RE.fullmatch(
                str(recipient.get("recipient", ""))
            ):
                issues.append(f"{path}: invalid age recipient stanza")
            if not str(recipient.get("enc", "")).startswith(
                "-----BEGIN AGE ENCRYPTED FILE-----"
            ):
                issues.append(f"{path}: wrapped age data key is missing")
    return issues


def load_document(path: Path, issues: list[str]) -> dict[str, Any] | None:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        issues.append(f"{path}: {exc}")
        return None
    if not isinstance(document, dict):
        issues.append(f"{path}: document must be a mapping")
        return None
    return document


def check(root: Path) -> list[str]:
    issues: list[str] = []
    desired_root = root / "desired"
    environments_root = desired_root / "environments"
    if not environments_root.is_dir():
        return [f"{environments_root}: missing environments directory"]

    environment_roots = sorted(
        path for path in environments_root.iterdir() if path.is_dir()
    )
    common_root = desired_root / "common"
    expected_yaml = {
        *(common_root / filename for filename in COMMON_FILES),
        desired_root / "fleet-ids.yml",
        *(path / "topology.sops.yml" for path in environment_roots),
    }
    actual_yaml = {
        *desired_root.rglob("*.yml"),
        *desired_root.rglob("*.yaml"),
    }
    for path in sorted(actual_yaml - expected_yaml):
        issues.append(f"{path}: unexpected or plaintext desired-state YAML")

    for filename in COMMON_FILES:
        path = common_root / filename
        document = load_document(path, issues)
        if document is None:
            continue
        payload = {key: value for key, value in document.items() if key != "sops"}
        issues.extend(f"{path}: {issue}" for issue in encrypted_leaves(payload, "$"))
        issues.extend(check_sops_metadata(path, document))

    fleet_ids = root / "desired" / "fleet-ids.yml"
    document = load_document(fleet_ids, issues)
    if document is not None:
        payload = {key: value for key, value in document.items() if key != "sops"}
        issues.extend(
            f"{fleet_ids}: {issue}" for issue in encrypted_leaves(payload, "$")
        )
        issues.extend(check_sops_metadata(fleet_ids, document))

    for environment_root in environment_roots:
        environment = environment_root.name
        if not ENVIRONMENT_RE.fullmatch(environment):
            issues.append(f"{environment_root}: invalid environment directory name")
            continue
        topology = environment_root / "topology.sops.yml"
        yaml_files = sorted((*environment_root.rglob("*.yml"), *environment_root.rglob("*.yaml")))
        unexpected = [path for path in yaml_files if path != topology]
        for path in unexpected:
            issues.append(f"{path}: plaintext or mixed environment YAML is forbidden")
        if not topology.is_file():
            issues.append(f"{topology}: missing encrypted topology")
            continue
        document = load_document(topology, issues)
        if document is None:
            continue
        if document.get("apiVersion") != "spiritvpn.io/v1alpha1":
            issues.append(f"{topology}: unsupported apiVersion")
        if document.get("kind") != "EnvironmentTopology":
            issues.append(f"{topology}: kind must be EnvironmentTopology")
        if document.get("metadata") != {"id": environment}:
            issues.append(f"{topology}: metadata.id must match the directory")
        spec = document.get("spec")
        if not isinstance(spec, dict) or "objects" not in spec:
            issues.append(f"{topology}: encrypted spec.objects envelope is missing")
        else:
            issues.extend(f"{topology}: {issue}" for issue in encrypted_leaves(spec, "spec"))

        issues.extend(check_sops_metadata(topology, document))
        sops = document.get("sops")
        if not isinstance(sops, dict):
            continue
        if sops.get("encrypted_regex") != "^(spec)$":
            issues.append(f"{topology}: SOPS encrypted_regex must cover spec")
    return issues


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    arguments = parser.parse_args()
    issues = check(arguments.root.resolve())
    if issues:
        for issue in issues:
            print(issue)
        raise SystemExit(2)
    print("encrypted desired-state envelopes: valid")


if __name__ == "__main__":
    main()
