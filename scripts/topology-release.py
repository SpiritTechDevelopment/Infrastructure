#!/usr/bin/env python3
"""Pack and update SOPS environment topology without writing plaintext to disk."""

from __future__ import annotations

import argparse
import copy
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml


API_VERSION = "spiritvpn.io/v1alpha1"
OBJECT_KINDS = {"Environment", "Fleet", "LogicalNode", "Instance"}
ENVIRONMENT_RE = re.compile(r"^[a-z0-9-]{1,63}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY_RE = re.compile(r"^ghcr\.io/[a-z0-9._/-]+$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def fail(message: str) -> None:
    raise SystemExit(f"topology release failed: {message}")


def load_mapping(text: str, source: str) -> dict[str, Any]:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        fail(f"{source} is not valid YAML: {exc}")
    if not isinstance(document, dict):
        fail(f"{source} must be a YAML mapping")
    return document


def run_sops(arguments: list[str], *, content: str | None = None) -> str:
    try:
        result = subprocess.run(
            ["sops", *arguments],
            input=content,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        fail(f"cannot execute sops: {exc}")
    if result.returncode != 0:
        diagnostic = result.stderr.strip().splitlines()
        fail(diagnostic[-1] if diagnostic else "sops failed without a diagnostic")
    return result.stdout


def topology_path(root: Path, environment: str) -> Path:
    if not ENVIRONMENT_RE.fullmatch(environment):
        fail("invalid environment name")
    return root / "desired" / "environments" / environment / "topology.sops.yml"


def encrypt(root: Path, target: Path, document: dict[str, Any]) -> str:
    try:
        relative = target.relative_to(root)
    except ValueError:
        fail("topology target must be inside the repository")
    plaintext = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
    return run_sops(
        [
            "--config",
            str(root / ".sops.yaml"),
            "--filename-override",
            str(relative),
            "--encrypt",
            "--input-type",
            "yaml",
            "--output-type",
            "yaml",
            "/dev/stdin",
        ],
        content=plaintext,
    )


def pack(root: Path, environment: str) -> str:
    environment_root = root / "desired" / "environments" / environment
    target = topology_path(root, environment)
    if not environment_root.is_dir():
        fail(f"environment directory does not exist: {environment}")
    if target.exists():
        fail(f"encrypted topology already exists for {environment}")

    objects: list[dict[str, Any]] = []
    paths = sorted((*environment_root.rglob("*.yml"), *environment_root.rglob("*.yaml")))
    for path in paths:
        document = load_mapping(path.read_text(encoding="utf-8"), str(path))
        if document.get("apiVersion") != API_VERSION or document.get("kind") not in OBJECT_KINDS:
            fail(f"unsupported desired-state object in {path}")
        objects.append(document)
    if not objects:
        fail(f"environment {environment} contains no desired-state objects")

    topology = {
        "apiVersion": API_VERSION,
        "kind": "EnvironmentTopology",
        "metadata": {"id": environment},
        "spec": {"objects": objects},
    }
    return encrypt(root, target, topology)


def require_release_value(value: str, pattern: re.Pattern[str], name: str) -> None:
    if not pattern.fullmatch(value):
        fail(f"invalid {name}")


def bump(arguments: argparse.Namespace) -> str:
    root = arguments.root.resolve()
    target = topology_path(root, arguments.environment)
    if not target.is_file():
        fail(f"encrypted topology does not exist for {arguments.environment}")

    require_release_value(arguments.release_source_git_sha, SHA_RE, "release source Git SHA")
    require_release_value(arguments.repository, REPOSITORY_RE, "image repository")
    require_release_value(arguments.digest, DIGEST_RE, "image digest")

    decrypted = run_sops(["--decrypt", str(target)])
    topology = load_mapping(decrypted, str(target))
    if topology.get("kind") != "EnvironmentTopology":
        fail("encrypted document is not EnvironmentTopology")
    objects = (topology.get("spec") or {}).get("objects")
    if not isinstance(objects, list):
        fail("topology spec.objects must be a list")
    environments = [
        item
        for item in objects
        if isinstance(item, dict)
        and item.get("kind") == "Environment"
        and (item.get("metadata") or {}).get("id") == arguments.environment
    ]
    if len(environments) != 1:
        fail("topology must contain exactly one matching Environment object")

    before = copy.deepcopy(topology)
    spec = environments[0].get("spec")
    if not isinstance(spec, dict):
        fail("Environment spec must be a mapping")

    if arguments.component == "control-release":
        require_release_value(arguments.secondary_repository, REPOSITORY_RE, "migration repository")
        require_release_value(arguments.secondary_digest, DIGEST_RE, "migration digest")
        try:
            release = spec["control"]["backend_release"]
        except (KeyError, TypeError):
            fail("environment does not declare an existing control release")
        release["source_git_sha"] = arguments.release_source_git_sha
        release["backend_image"] = {
            "repository": arguments.repository,
            "digest": arguments.digest,
        }
        release["migration_image"] = {
            "repository": arguments.secondary_repository,
            "digest": arguments.secondary_digest,
        }
    elif arguments.component == "agent-release":
        if arguments.secondary_repository != "-" or arguments.secondary_digest != "-":
            fail("agent release must not carry a secondary image")
        components = spec.setdefault("common_overrides", {}).setdefault("components", {})
        components["node_agent"] = {
            "repository": arguments.repository,
            "digest": arguments.digest,
        }
    elif arguments.component == "bot-release":
        if arguments.secondary_repository != "-" or arguments.secondary_digest != "-":
            fail("bot release must not carry a secondary image")
        try:
            release = spec["control"]["bot"]["release"]
        except (KeyError, TypeError):
            fail("environment does not declare an existing bot release")
        release["source_git_sha"] = arguments.release_source_git_sha
        release["image"] = {
            "repository": arguments.repository,
            "digest": arguments.digest,
        }
    else:
        fail(f"unsupported component: {arguments.component}")

    if topology == before:
        fail("release payload does not change topology")
    return encrypt(root, target, topology)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    subcommands = result.add_subparsers(dest="command", required=True)

    pack_parser = subcommands.add_parser("pack")
    pack_parser.add_argument("--root", type=Path, default=Path.cwd())
    pack_parser.add_argument("--environment", required=True)
    pack_parser.add_argument("--output", type=Path)

    bump_parser = subcommands.add_parser("bump")
    bump_parser.add_argument("--root", type=Path, default=Path.cwd())
    bump_parser.add_argument("--environment", required=True)
    bump_parser.add_argument(
        "--component",
        required=True,
        choices=("control-release", "agent-release", "bot-release"),
    )
    bump_parser.add_argument("--release-source-git-sha", required=True)
    bump_parser.add_argument("--repository", required=True)
    bump_parser.add_argument("--digest", required=True)
    bump_parser.add_argument("--secondary-repository", default="-")
    bump_parser.add_argument("--secondary-digest", default="-")
    return result


def main() -> None:
    arguments = parser().parse_args()
    root = arguments.root.resolve()
    if arguments.command == "pack":
        output = pack(root, arguments.environment)
    else:
        output = bump(arguments)
    if arguments.command == "pack" and arguments.output is not None:
        target = topology_path(root, arguments.environment).resolve()
        if arguments.output.resolve() != target:
            fail("pack output must be the environment topology.sops.yml path")
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except OSError as exc:
            fail(f"cannot create encrypted topology: {exc}")
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(output)
    else:
        sys.stdout.write(output)


if __name__ == "__main__":
    main()
