"""Command-line interface for fleetctl."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from fleetctl.adapters import OutputDirectoryError, write_generated_artifact, write_rendered_files
from fleetctl.compiler import render_files
from fleetctl.planning import PlanningError, build_impact_plan
from fleetctl.validation import DesiredStateInvalid, validate_environment


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fleetctl")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root (default: current directory)")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate one environment without network access")
    validate.add_argument("--environment", required=True, choices=("develop", "staging", "prod"))
    render = commands.add_parser("render", help="render deterministic local artifacts without network access")
    render.add_argument("--environment", required=True, choices=("develop", "staging", "prod"))
    render.add_argument("--output", required=True, type=Path)
    plan = commands.add_parser("plan", help="compare desired state with an explicit baseline directory")
    plan.add_argument("--environment", required=True, choices=("develop", "staging", "prod"))
    plan.add_argument("--baseline", required=True, type=Path, help="baseline desired/ directory")
    plan.add_argument("--output", type=Path, help="fleetctl-managed build directory; JSON is printed when omitted")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate":
        try:
            state = validate_environment(args.root, args.environment)
        except DesiredStateInvalid as exc:
            for issue in exc.issues:
                print(issue.render(), file=sys.stderr)
            print(f"{args.environment}: invalid ({len(exc.issues)} error(s))", file=sys.stderr)
            return 1
        print(
            f"{args.environment}: valid "
            f"({len(state.fleets)} fleets, {len(state.nodes)} nodes, {len(state.instances)} instances)"
        )
        return 0
    if args.command == "render":
        try:
            state = validate_environment(args.root, args.environment)
            files = render_files(state)
            write_rendered_files(args.output, files)
        except DesiredStateInvalid as exc:
            for issue in exc.issues:
                print(issue.render(), file=sys.stderr)
            print(f"{args.environment}: invalid ({len(exc.issues)} error(s))", file=sys.stderr)
            return 1
        except OutputDirectoryError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"{args.environment}: rendered {len(files)} artifact(s) to {args.output}")
        return 0
    if args.command == "plan":
        try:
            current = validate_environment(args.root, args.environment)
            baseline = validate_environment(args.root, args.environment, desired_root=args.baseline)
            plan = build_impact_plan(current, baseline)
            payload = plan.to_json_bytes()
            if args.output is None:
                print(payload.decode("utf-8"), end="")
            else:
                target = write_generated_artifact(args.output, "impact-plan.json", payload)
                print(f"{args.environment}: planned {len(plan.changes)} change(s) to {target}")
            return 0
        except DesiredStateInvalid as exc:
            for issue in exc.issues:
                print(issue.render(), file=sys.stderr)
            print(f"{args.environment}: invalid ({len(exc.issues)} error(s))", file=sys.stderr)
            return 1
        except (PlanningError, OutputDirectoryError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
    raise AssertionError(f"unreachable command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
