#!/usr/bin/env python3
"""Materialize platform image pins from the encrypted common desired state."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PLATFORM_COMPONENT_VARIABLES = {
    "vault": "platform_vault_image",
    "prometheus": "platform_prometheus_image",
    "node_exporter": "platform_node_exporter_image",
    "grafana": "platform_grafana_image",
    "alertmanager": "platform_alertmanager_image",
    "loki": "platform_loki_image",
    "alloy": "platform_alloy_image",
}


class PlatformComponentError(Exception):
    """The canonical component declaration cannot drive the platform roles."""


def _load_document(path: Path) -> dict[str, Any]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PlatformComponentError("common component desired state is unreadable") from exc
    if isinstance(document, dict) and "sops" in document:
        sops = shutil.which("sops")
        if sops is None:
            raise PlatformComponentError("sops is required to read common component desired state")
        result = subprocess.run(
            [sops, "--decrypt", str(path)],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise PlatformComponentError("common component desired state cannot be decrypted")
        try:
            document = yaml.safe_load(result.stdout)
        except yaml.YAMLError as exc:
            raise PlatformComponentError("decrypted common component desired state is invalid YAML") from exc
    if not isinstance(document, dict):
        raise PlatformComponentError("common component desired state must be a mapping")
    return document


def platform_component_variables(components_path: Path, schema_path: Path) -> dict[str, str]:
    document = _load_document(components_path)
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except (OSError, ValueError) as exc:
        raise PlatformComponentError("component schema is unavailable or invalid") from exc
    errors = sorted(
        Draft202012Validator(schema).iter_errors(document),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        raise PlatformComponentError("common component desired state does not satisfy its schema")

    components = document["components"]
    missing = sorted(set(PLATFORM_COMPONENT_VARIABLES) - set(components))
    if missing:
        raise PlatformComponentError(
            "common desired state is missing platform components: " + ", ".join(missing)
        )

    variables: dict[str, str] = {}
    for component_name, variable_name in PLATFORM_COMPONENT_VARIABLES.items():
        component = components[component_name]
        digest = component["digest"]
        if digest is None:
            raise PlatformComponentError(
                f"platform component {component_name!r} requires an immutable digest"
            )
        variables[variable_name] = (
            f"{component['repository']}:{component['tag']}@{digest}"
        )
    return variables


def write_private(path: Path, variables: dict[str, str]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            yaml.safe_dump(variables, stream, allow_unicode=True, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--components",
        type=Path,
        default=REPOSITORY_ROOT / "desired" / "common" / "components.yml",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=REPOSITORY_ROOT / "contracts" / "desired-state" / "components.schema.json",
    )
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        variables = platform_component_variables(args.components, args.schema)
        write_private(args.output, variables)
    except PlatformComponentError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
