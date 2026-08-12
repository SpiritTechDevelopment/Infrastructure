"""Command-line interface for fleetctl."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from fleetctl.adapters import (
    CompiledArtifactsError,
    GitAdapterError,
    GitRepository,
    OutputDirectoryError,
    PlatformArtifactsError,
    write_generated_artifact,
    write_rendered_files,
    validate_ansible_artifacts,
    validate_platform_artifacts,
    validate_platform_known_hosts,
)
from fleetctl.compiler import PlatformNotDeclared, render_files, render_platform_files
from fleetctl.deployment import DeploymentCoordinator, DeploymentError, DeploymentOptions
from fleetctl.planning import PlanningError, build_impact_plan, build_initial_baseline
from fleetctl.provisioning import ManualProvisioningAdapter
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
    platform_render = commands.add_parser(
        "platform-render", help="render a deterministic manual platform-bootstrap plan"
    )
    platform_render.add_argument("--environment", required=True, choices=("develop", "staging", "prod"))
    platform_render.add_argument("--output", required=True, type=Path)
    platform_check = commands.add_parser(
        "platform-check", help="validate generated platform inputs without SSH or Vault access"
    )
    platform_check.add_argument("--environment", required=True, choices=("develop", "staging", "prod"))
    platform_check.add_argument("--build-dir", required=True, type=Path)
    platform_known_hosts = commands.add_parser(
        "platform-known-hosts-check", help="match pinned known_hosts to reviewed platform fingerprints"
    )
    platform_known_hosts.add_argument("--build-dir", required=True, type=Path)
    platform_known_hosts.add_argument("--known-hosts", required=True, type=Path)
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
    ansible_check = commands.add_parser(
        "ansible-check",
        help="validate generated inventory and node-plan inputs without SSH",
    )
    ansible_check.add_argument("--environment", required=True, choices=("develop", "staging", "prod"))
    ansible_check.add_argument("--build-dir", required=True, type=Path)
    provisioning_check = commands.add_parser(
        "provisioning-check",
        help="run provider-neutral manual provisioning preflight without external actions",
    )
    provisioning_check.add_argument("--environment", required=True, choices=("develop", "staging", "prod"))
    deploy = commands.add_parser(
        "deploy",
        help="run infrastructure workflow; dry-run is the default and stops at WAITING_FOR_BACKEND",
    )
    deploy.add_argument("--environment", required=True, choices=("develop", "staging", "prod"))
    deploy.add_argument("--source", default="HEAD")
    deploy.add_argument("--initial", action="store_true")
    deploy.add_argument("--apply", action="store_true", help="explicitly allow Ansible SSH/mutation")
    deploy.add_argument("--resume", action="store_true")
    deploy.add_argument("--build-dir", type=Path)
    deploy.add_argument("--bootstrap-vars", type=Path)
    deploy.add_argument("--compiled-secrets", type=Path)
    deploy.add_argument("--readiness-vars", type=Path)
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
    if args.command == "platform-render":
        try:
            state = validate_environment(args.root, args.environment)
            files = render_platform_files(state)
            write_rendered_files(args.output, files)
        except DesiredStateInvalid as exc:
            for issue in exc.issues:
                print(issue.render(), file=sys.stderr)
            return 1
        except (PlatformNotDeclared, OutputDirectoryError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"{args.environment}: rendered platform bootstrap artifacts to {args.output}")
        return 0
    if args.command == "platform-check":
        try:
            host = validate_platform_artifacts(args.build_dir, args.environment)
        except PlatformArtifactsError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"{args.environment}: generated platform input valid ({host})")
        return 0
    if args.command == "platform-known-hosts-check":
        try:
            fingerprint = validate_platform_known_hosts(args.build_dir, args.known_hosts)
        except PlatformArtifactsError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"platform SSH host key matches reviewed fingerprint {fingerprint}")
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
    if args.command == "ansible-check":
        try:
            host_count = validate_ansible_artifacts(args.build_dir, args.environment)
        except CompiledArtifactsError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"{args.environment}: generated Ansible input valid ({host_count} host(s))")
        return 0
    if args.command == "provisioning-check":
        try:
            state = validate_environment(args.root, args.environment)
        except DesiredStateInvalid as exc:
            for issue in exc.issues:
                print(issue.render(), file=sys.stderr)
            return 1
        adapter = ManualProvisioningAdapter()
        reports = [
            adapter.preflight(instance)
            for instance in sorted(state.instances, key=lambda item: item.object_id)
        ]
        payload = {
            "_notice": "GENERATED — DO NOT EDIT",
            "schema_version": 1,
            "environment": args.environment,
            "passed": all(report.passed for report in reports),
            "instances": [report.to_dict() for report in reports],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if payload["passed"] else 1
    if args.command == "deploy":
        try:
            record = DeploymentCoordinator(args.root).run(
                DeploymentOptions(
                    environment=args.environment,
                    source=args.source,
                    initial=args.initial,
                    apply=args.apply,
                    resume=args.resume,
                    build_directory=args.build_dir,
                    bootstrap_vars=args.bootstrap_vars,
                    compiled_secrets=args.compiled_secrets,
                    readiness_vars=args.readiness_vars,
                )
            )
        except (DeploymentError, GitAdapterError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    raise AssertionError(f"unreachable command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
