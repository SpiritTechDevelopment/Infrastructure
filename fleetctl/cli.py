"""Command-line interface for fleetctl."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from fleetctl.adapters import (
    GitAdapterError,
    GitRepository,
    OutputDirectoryError,
    write_generated_artifact,
    write_rendered_files,
)
from fleetctl.compiler import render_files
from fleetctl.planning import PlanningError, build_impact_plan, build_initial_baseline
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
    plan = commands.add_parser("plan", help="compare a Git source with the last deployed baseline")
    plan.add_argument("--environment", required=True, choices=("develop", "staging", "prod"))
    baseline_mode = plan.add_mutually_exclusive_group()
    baseline_mode.add_argument("--baseline", type=Path, help="explicit baseline desired/ directory (tests only)")
    baseline_mode.add_argument(
        "--initial",
        action="store_true",
        help="explicitly plan a first deployment; fails if the deployment ref exists",
    )
    plan.add_argument("--source", default="HEAD", help="source commit or ref (default: HEAD)")
    plan.add_argument("--output", type=Path, help="fleetctl-managed build directory; JSON is printed when omitted")
    update_ref = commands.add_parser(
        "update-deployment-ref",
        help="atomically record a separately verified successful deployment",
    )
    update_ref.add_argument("--environment", required=True, choices=("develop", "staging", "prod"))
    update_ref.add_argument("--source-git-sha", required=True)
    expected = update_ref.add_mutually_exclusive_group(required=True)
    expected.add_argument("--expected-baseline-git-sha")
    expected.add_argument("--initial", action="store_true")
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
            if args.baseline is not None:
                if args.source != "HEAD":
                    raise GitAdapterError("--source cannot be combined with the explicit --baseline test mode")
                current = validate_environment(args.root, args.environment)
                baseline = validate_environment(args.root, args.environment, desired_root=args.baseline)
                impact_plan = build_impact_plan(current, baseline)
            else:
                repository = GitRepository(args.root)
                source_git_sha = repository.resolve_commit(args.source)
                repository.assert_desired_matches_commit(source_git_sha)
                baseline_git_sha = repository.resolve_deployment_baseline(args.environment)
                if baseline_git_sha is None and not args.initial:
                    raise GitAdapterError(
                        f"deployment baseline {repository.deployment_ref(args.environment)} is missing; "
                        "use --initial only for an intentional first deployment"
                    )
                if baseline_git_sha is not None and args.initial:
                    raise GitAdapterError(
                        f"--initial refused: deployment baseline already exists at {baseline_git_sha}"
                    )
                with repository.materialize_desired(source_git_sha) as source_desired:
                    current = validate_environment(
                        args.root,
                        args.environment,
                        desired_root=source_desired,
                    )
                if baseline_git_sha is None:
                    baseline = build_initial_baseline(current)
                else:
                    with repository.materialize_desired(baseline_git_sha) as baseline_desired:
                        baseline = validate_environment(
                            args.root,
                            args.environment,
                            desired_root=baseline_desired,
                        )
                impact_plan = build_impact_plan(
                    current,
                    baseline,
                    source_git_sha=source_git_sha,
                    baseline_git_sha=baseline_git_sha,
                    initial_deployment=baseline_git_sha is None,
                )
            payload = impact_plan.to_json_bytes()
            if args.output is None:
                print(payload.decode("utf-8"), end="")
            else:
                target = write_generated_artifact(args.output, "impact-plan.json", payload)
                print(f"{args.environment}: planned {len(impact_plan.changes)} change(s) to {target}")
            return 0
        except DesiredStateInvalid as exc:
            for issue in exc.issues:
                print(issue.render(), file=sys.stderr)
            print(f"{args.environment}: invalid ({len(exc.issues)} error(s))", file=sys.stderr)
            return 1
        except (GitAdapterError, PlanningError, OutputDirectoryError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if args.command == "update-deployment-ref":
        try:
            repository = GitRepository(args.root)
            source_git_sha = repository.update_deployment_ref(
                args.environment,
                args.source_git_sha,
                expected_baseline=(
                    None if args.initial else args.expected_baseline_git_sha
                ),
            )
        except GitAdapterError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(
            f"{repository.deployment_ref(args.environment)} atomically updated to {source_git_sha}"
        )
        return 0
    raise AssertionError(f"unreachable command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
