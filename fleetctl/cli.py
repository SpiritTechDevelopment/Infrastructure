"""CLI для fleetctl."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from fleetctl.adapters import (
    CompiledArtifactsError,
    CloudflareClient,
    CloudflareDnsError,
    GitAdapterError,
    GitRepository,
    OutputDirectoryError,
    read_cloudflare_token,
    reconcile_cloudflare_dns,
    write_generated_artifact,
    write_rendered_files,
    validate_ansible_artifacts,
)
from fleetctl.compiler import (
    BackendManifestError,
    backend_manifest_bytes,
    compile_backend_manifest,
    compile_dns_plan,
    render_files,
)
from fleetctl.deployment import DeploymentCoordinator, DeploymentError, DeploymentOptions
from fleetctl.model import DesiredState
from fleetctl.pki import DEFAULT_VALIDITY_DAYS, PkiError
from fleetctl.pki.issuance import CONTROL_PROFILES, issue_control_certificate, sign_agent_certificate
from fleetctl.planning import ImpactPlan, PlanningError, build_impact_plan, build_initial_baseline
from fleetctl.provisioning import ManualProvisioningAdapter
from fleetctl.validation import DesiredStateInvalid, validate_environment


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fleetctl")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root (default: current directory)")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate one environment without network access")
    validate.add_argument("--environment", required=True, choices=("develop", "prod"))
    render = commands.add_parser("render", help="render deterministic local artifacts without network access")
    render.add_argument("--environment", required=True, choices=("develop", "prod"))
    render.add_argument("--output", required=True, type=Path)
    plan = commands.add_parser("plan", help="compare a Git source with the last deployed baseline")
    plan.add_argument("--environment", required=True, choices=("develop", "prod"))
    baseline_mode = plan.add_mutually_exclusive_group()
    baseline_mode.add_argument("--baseline", type=Path, help="explicit baseline desired/ directory (tests only)")
    baseline_mode.add_argument(
        "--initial",
        action="store_true",
        help="explicitly plan a first deployment; fails if the deployment ref exists",
    )
    plan.add_argument("--source", default="HEAD", help="source commit or ref (default: HEAD)")
    plan.add_argument("--output", type=Path, help="fleetctl-managed build directory; JSON is printed when omitted")
    check_change = commands.add_parser(
        "check-change",
        help="validate and compile the exact transition from the deployment ref",
    )
    check_change.add_argument("--environment", required=True, choices=("develop", "prod"))
    check_change.add_argument("--source", default="HEAD", help="source commit or ref (default: HEAD)")
    check_change.add_argument(
        "--initial",
        action="store_true",
        help="check an intentional first deployment when no deployment ref exists",
    )
    check_change.add_argument("--output", required=True, type=Path)
    manifest = commands.add_parser(
        "manifest",
        help="render a deployment-scoped backend manifest without making an RPC",
    )
    manifest.add_argument("--environment", required=True, choices=("develop", "prod"))
    manifest_baseline = manifest.add_mutually_exclusive_group()
    manifest_baseline.add_argument("--baseline", type=Path, help="explicit baseline desired/ directory (tests only)")
    manifest_baseline.add_argument(
        "--initial",
        action="store_true",
        help="explicitly render a first deployment; fails if the deployment ref exists",
    )
    manifest.add_argument("--source", default="HEAD", help="source commit or ref (default: HEAD)")
    manifest.add_argument("--revision", required=True, type=int)
    manifest.add_argument("--allow-destructive", action="store_true")
    manifest.add_argument("--output", type=Path, help="fleetctl-managed build directory; JSON is printed when omitted")
    update_ref = commands.add_parser(
        "update-deployment-ref",
        help="atomically record a separately verified successful deployment",
    )
    update_ref.add_argument("--environment", required=True, choices=("develop", "prod"))
    update_ref.add_argument("--source-git-sha", required=True)
    expected = update_ref.add_mutually_exclusive_group(required=True)
    expected.add_argument("--expected-baseline-git-sha")
    expected.add_argument("--initial", action="store_true")
    ansible_check = commands.add_parser(
        "ansible-check",
        help="validate generated inventory and node-plan inputs without SSH",
    )
    ansible_check.add_argument("--environment", required=True, choices=("develop", "prod"))
    ansible_check.add_argument("--build-dir", required=True, type=Path)
    provisioning_check = commands.add_parser(
        "provisioning-check",
        help="run provider-neutral manual provisioning preflight without external actions",
    )
    provisioning_check.add_argument("--environment", required=True, choices=("develop", "prod"))
    dns = commands.add_parser(
        "dns",
        help="plan or explicitly apply compiled DNS records through Cloudflare",
    )
    dns.add_argument("--environment", required=True, choices=("develop", "prod"))
    dns.add_argument(
        "--token-file",
        type=Path,
        help="mode-0600 Cloudflare API token file; otherwise CLOUDFLARE_API_TOKEN is used",
    )
    dns.add_argument("--apply", action="store_true", help="create or update records")
    deploy = commands.add_parser(
        "deploy",
        help="run infrastructure workflow; dry-run is the default and stops at WAITING_FOR_BACKEND",
    )
    deploy.add_argument("--environment", required=True, choices=("develop", "prod"))
    deploy.add_argument("--source", default="HEAD")
    deploy.add_argument("--initial", action="store_true")
    deploy.add_argument("--apply", action="store_true", help="explicitly allow Ansible SSH/mutation")
    deploy.add_argument("--resume", action="store_true")
    deploy.add_argument(
        "--allow-destructive",
        action="store_true",
        help="allow a manifest that removes nodes, fleet membership, or bridges",
    )
    deploy.add_argument("--build-dir", type=Path)
    deploy.add_argument(
        "--state-dir",
        type=Path,
        help="durable local coordinator state (default: <root>/.fleetctl-state)",
    )
    deploy.add_argument("--bootstrap-vars", type=Path)
    deploy.add_argument("--compiled-secrets", type=Path)
    deploy.add_argument("--readiness-vars", type=Path)
    deploy.add_argument(
        "--cloudflare-token-file",
        type=Path,
        help="mode-0600 token used to reconcile DNS after readiness and backend apply",
    )
    deploy.add_argument(
        "--backend-client-certificate",
        type=Path,
        help="manifest-writer certificate; without it the workflow stops before the RPC",
    )
    deploy.add_argument("--backend-client-private-key", type=Path)
    deploy.add_argument("--backend-ca", type=Path, help="CA that signs the backend server")
    deploy.add_argument(
        "--ca-state",
        type=Path,
        help="CA state directory; required only when the plan bootstraps nodes",
    )
    pki_issue = commands.add_parser(
        "pki-issue",
        help="issue a control-plane certificate; names are taken from desired state",
    )
    pki_issue.add_argument("--environment", required=True, choices=("develop", "prod"))
    pki_issue.add_argument("--profile", required=True, choices=CONTROL_PROFILES)
    _add_ca_arguments(pki_issue)
    pki_sign = commands.add_parser(
        "pki-sign",
        help="sign a node CSR for one declared instance; the node keeps its private key",
    )
    pki_sign.add_argument("--environment", required=True, choices=("develop", "prod"))
    pki_sign.add_argument("--instance", required=True)
    pki_sign.add_argument("--csr", required=True, type=Path, help="CSR reported by roles/pki_agent")
    _add_ca_arguments(pki_sign)
    return parser


def _add_ca_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--ca-state",
        required=True,
        type=Path,
        help="environment-scoped CA state directory; must live outside the repository",
    )
    command.add_argument("--output", required=True, type=Path)
    command.add_argument("--validity-days", type=int, default=DEFAULT_VALIDITY_DAYS)


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
            _, impact_plan = _prepare_plan(
                args.root,
                args.environment,
                source=args.source,
                baseline_path=args.baseline,
                initial=args.initial,
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
    if args.command == "check-change":
        try:
            current, impact_plan = _prepare_plan(
                args.root,
                args.environment,
                source=args.source,
                baseline_path=None,
                initial=args.initial,
            )
            files = render_files(current)
            write_rendered_files(args.output, files)
            host_count = validate_ansible_artifacts(args.output, args.environment)
            target = write_generated_artifact(
                args.output,
                "impact-plan.json",
                impact_plan.to_json_bytes(),
            )
        except DesiredStateInvalid as exc:
            for issue in exc.issues:
                print(issue.render(), file=sys.stderr)
            print(f"{args.environment}: invalid ({len(exc.issues)} error(s))", file=sys.stderr)
            return 1
        except (
            CompiledArtifactsError,
            GitAdapterError,
            PlanningError,
            OutputDirectoryError,
        ) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        summary = {
            "environment": args.environment,
            "source_git_sha": impact_plan.source_git_sha,
            "baseline_git_sha": impact_plan.baseline_git_sha,
            "initial_deployment": impact_plan.initial_deployment,
            "transition_kinds": list(impact_plan.transition_kinds),
            "change_count": len(impact_plan.changes),
            "destructive": impact_plan.destructive,
            "compiled_host_count": host_count,
            "impact_plan": str(target),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "manifest":
        try:
            current, impact_plan = _prepare_plan(
                args.root,
                args.environment,
                source=args.source,
                baseline_path=args.baseline,
                initial=args.initial,
            )
            request = compile_backend_manifest(
                current,
                impact_plan,
                revision=args.revision,
                allow_destructive=args.allow_destructive,
            )
            payload = backend_manifest_bytes(request)
            if args.output is None:
                print(payload.decode("utf-8"), end="")
            else:
                target = write_generated_artifact(args.output, "backend-manifest.json", payload)
                print(f"{args.environment}: backend manifest revision {args.revision} rendered to {target}")
            return 0
        except DesiredStateInvalid as exc:
            for issue in exc.issues:
                print(issue.render(), file=sys.stderr)
            print(f"{args.environment}: invalid ({len(exc.issues)} error(s))", file=sys.stderr)
            return 1
        except (BackendManifestError, GitAdapterError, PlanningError, OutputDirectoryError) as exc:
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
    if args.command == "dns":
        try:
            state = validate_environment(args.root, args.environment)
            if args.apply:
                repository = GitRepository(args.root)
                source_git_sha = repository.resolve_commit("HEAD")
                repository.assert_desired_matches_commit(source_git_sha)
            token = read_cloudflare_token(args.token_file)
            result = reconcile_cloudflare_dns(
                compile_dns_plan(state),
                CloudflareClient(token),
                apply=args.apply,
            )
        except DesiredStateInvalid as exc:
            for issue in exc.issues:
                print(issue.render(), file=sys.stderr)
            return 1
        except (CloudflareDnsError, GitAdapterError, OSError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "deploy":
        try:
            record = DeploymentCoordinator(args.root).run(
                DeploymentOptions(
                    environment=args.environment,
                    source=args.source,
                    initial=args.initial,
                    apply=args.apply,
                    resume=args.resume,
                    allow_destructive=args.allow_destructive,
                    build_directory=args.build_dir,
                    state_directory=args.state_dir,
                    bootstrap_vars=args.bootstrap_vars,
                    compiled_secrets=args.compiled_secrets,
                    readiness_vars=args.readiness_vars,
                    cloudflare_token_file=args.cloudflare_token_file,
                    ca_state=args.ca_state,
                    backend_client_certificate=args.backend_client_certificate,
                    backend_client_private_key=args.backend_client_private_key,
                    backend_certificate_authority=args.backend_ca,
                )
            )
        except (DeploymentError, GitAdapterError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command in ("pki-issue", "pki-sign"):
        try:
            state = validate_environment(args.root, args.environment)
        except DesiredStateInvalid as exc:
            for issue in exc.issues:
                print(issue.render(), file=sys.stderr)
            print(f"{args.environment}: invalid ({len(exc.issues)} error(s))", file=sys.stderr)
            return 1
        try:
            if args.command == "pki-issue":
                summary = issue_control_certificate(
                    state,
                    args.profile,
                    ca_state=args.ca_state,
                    output=args.output,
                    validity_days=args.validity_days,
                )
            else:
                summary = sign_agent_certificate(
                    state,
                    args.instance,
                    ca_state=args.ca_state,
                    csr_pem=args.csr.read_bytes(),
                    output=args.output,
                    validity_days=args.validity_days,
                )
        except (PkiError, OSError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    raise AssertionError(f"unreachable command: {args.command}")


def _prepare_plan(
    root: Path,
    environment: str,
    *,
    source: str,
    baseline_path: Path | None,
    initial: bool,
) -> tuple[DesiredState, ImpactPlan]:
    if baseline_path is not None:
        if source != "HEAD":
            raise GitAdapterError("--source cannot be combined with the explicit --baseline test mode")
        current = validate_environment(root, environment)
        baseline = validate_environment(root, environment, desired_root=baseline_path)
        return current, build_impact_plan(current, baseline)

    repository = GitRepository(root)
    source_git_sha = repository.resolve_commit(source)
    repository.assert_desired_matches_commit(source_git_sha)
    baseline_git_sha = repository.resolve_deployment_baseline(environment)
    if baseline_git_sha is None and not initial:
        raise GitAdapterError(
            f"deployment baseline {repository.deployment_ref(environment)} is missing; "
            "use --initial only for an intentional first deployment"
        )
    if baseline_git_sha is not None and initial:
        raise GitAdapterError(
            f"--initial refused: deployment baseline already exists at {baseline_git_sha}"
        )
    with repository.materialize_desired(source_git_sha) as source_desired:
        current = validate_environment(root, environment, desired_root=source_desired)
    if baseline_git_sha is None:
        baseline = build_initial_baseline(current)
    else:
        with repository.materialize_desired(baseline_git_sha) as baseline_desired:
            baseline = validate_environment(root, environment, desired_root=baseline_desired)
    plan = build_impact_plan(
        current,
        baseline,
        source_git_sha=source_git_sha,
        baseline_git_sha=baseline_git_sha,
        initial_deployment=baseline_git_sha is None,
    )
    return current, plan


if __name__ == "__main__":
    raise SystemExit(main())
