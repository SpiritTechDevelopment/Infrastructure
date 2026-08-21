#!/usr/bin/env python3
"""Validate the compiled control operator contract and its applied approval."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml


class ControlContractError(Exception):
    """The compiled or explicitly applied control contract is unusable."""


def _load_control_plan(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControlContractError("compiled control plan is unreadable") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ControlContractError("compiled control plan has an unsupported structure")
    return document


def desired_contract(plan_path: Path) -> dict[str, Any]:
    plan = _load_control_plan(plan_path)
    try:
        command = plan["postgres"]["external_backup_command_argv"]
    except (KeyError, TypeError) as exc:
        raise ControlContractError("compiled control plan has no backup contract") from exc
    if (
        not isinstance(command, list)
        or len(command) > 32
        or any(not isinstance(item, str) for item in command)
        or (
            command
            and (
                not command[0].startswith("/")
                or any(item != item.strip() or "\n" in item for item in command)
            )
        )
    ):
        raise ControlContractError("compiled external backup argv is invalid")
    return {"control_external_backup_command_argv": command}


def require_applied_contract(plan_path: Path, applied_path: Path) -> None:
    desired = desired_contract(plan_path)
    try:
        applied = yaml.safe_load(applied_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ControlContractError("applied control approval contract is unreadable") from exc
    if not isinstance(applied, dict) or applied != desired:
        raise ControlContractError(
            "reviewed control backup contract differs from the explicitly applied approval; "
            "review and install the exact Git-owned contract before apply"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-plan", required=True, type=Path)
    parser.add_argument("--require-applied", type=Path)
    args = parser.parse_args()
    try:
        desired_contract(args.control_plan.resolve())
        if args.require_applied is not None:
            require_applied_contract(
                args.control_plan.resolve(),
                args.require_applied.resolve(),
            )
    except ControlContractError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
