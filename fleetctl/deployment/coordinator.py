"""Resume-safe infrastructure-only deployment workflow."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fleetctl.adapters import (
    BackendCallError,
    BackendEndpoint,
    GitRepository,
    apply_fleet_manifest,
    validate_ansible_artifacts,
    write_rendered_files,
)
from fleetctl.compiler import (
    backend_manifest_bytes,
    backend_manifest_payload_digest,
    compile_backend_manifest,
    render_files,
)
from fleetctl.compiler.addressing import control_management_address
from fleetctl.model import DesiredState
from fleetctl.pki import PkiError
from fleetctl.pki.issuance import issue_control_certificate, sign_agent_certificate
from fleetctl.planning import ImpactPlan, build_impact_plan, build_initial_baseline
from fleetctl.provisioning import ManualProvisioningAdapter
from fleetctl.validation import validate_environment

from .revisions import ManifestRevisionAllocator


class DeploymentError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class DeploymentOptions:
    environment: str
    source: str = "HEAD"
    initial: bool = False
    apply: bool = False
    resume: bool = False
    allow_destructive: bool = False
    build_directory: Path | None = None
    state_directory: Path | None = None
    bootstrap_vars: Path | None = None
    compiled_secrets: Path | None = None
    readiness_vars: Path | None = None
    # Root of the offline CA. Required only when nodes are actually bootstrapped,
    # because that is the only step that needs to sign anything.
    ca_state: Path | None = None
    # manifest-writer identity. Absent means the workflow stops at the contract
    # boundary exactly as it did before: compiled, allocated, not sent.
    backend_client_certificate: Path | None = None
    backend_client_private_key: Path | None = None
    backend_certificate_authority: Path | None = None
    backend_timeout_seconds: int = 60


class DeploymentCoordinator:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.repository = GitRepository(self.root)

    def run(self, options: DeploymentOptions) -> dict[str, Any]:
        source_git_sha = self.repository.resolve_commit(options.source)
        self.repository.assert_desired_matches_commit(source_git_sha)
        build_directory = (
            options.build_directory or self.root / "build" / options.environment
        ).resolve()
        state_directory = options.state_directory or self.root / ".fleetctl-state"
        if not state_directory.is_absolute():
            state_directory = self.root / state_directory
        record_directory, revision_directory, bootstrapped_directory = (
            self._prepare_state_directories(state_directory)
        )
        deployment_id = f"{options.environment}-{source_git_sha[:12]}"
        record_path = record_directory / f"{deployment_id}.json"
        lock_path = record_directory / f"{options.environment}.lock"

        with lock_path.open("a+", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DeploymentError(
                    f"environment {options.environment} already has an active deployment"
                ) from exc
            return self._run_locked(
                options,
                source_git_sha=source_git_sha,
                deployment_id=deployment_id,
                build_directory=build_directory,
                record_path=record_path,
                revision_state_path=(
                    revision_directory / f"{options.environment}.json"
                ),
                bootstrapped_directory=bootstrapped_directory,
                backend_identity_directory=state_directory / "backend-client",
            )

    def _run_locked(
        self,
        options: DeploymentOptions,
        *,
        source_git_sha: str,
        deployment_id: str,
        build_directory: Path,
        record_path: Path,
        revision_state_path: Path,
        bootstrapped_directory: Path,
        backend_identity_directory: Path,
    ) -> dict[str, Any]:
        existing = self._load_record(record_path)
        if existing is not None and not options.resume:
            raise DeploymentError(
                f"deployment record already exists: {record_path}; use --resume explicitly"
            )
        if existing is not None:
            if existing.get("source_git_sha") != source_git_sha:
                raise DeploymentError("resume record belongs to another source commit")
            record = existing
            record["dry_run"] = not options.apply
        else:
            record = {
                "schema_version": 1,
                "deployment_id": deployment_id,
                "environment": options.environment,
                "source_git_sha": source_git_sha,
                "baseline_git_sha": None,
                "dry_run": not options.apply,
                "status": "RUNNING",
                "deployment_ref_updated": False,
                "steps": [],
                "created_at": _timestamp(),
                "updated_at": _timestamp(),
            }
            self._write_record(record_path, record)

        try:
            with self.repository.materialize_desired(source_git_sha) as source_desired:
                current = validate_environment(
                    self.root,
                    options.environment,
                    desired_root=source_desired,
                )
            self._complete_step(record, record_path, "validate", "desired state is valid")

            baseline_git_sha = self.repository.resolve_deployment_baseline(options.environment)
            if baseline_git_sha is None and not options.initial:
                raise DeploymentError(
                    f"deployment baseline is missing; use --initial only for the intentional first deployment"
                )
            if baseline_git_sha is not None and options.initial:
                raise DeploymentError("--initial refused because a deployment baseline already exists")
            record["baseline_git_sha"] = baseline_git_sha
            self._complete_step(
                record,
                record_path,
                "resolve_git_baseline",
                "explicit initial deployment" if baseline_git_sha is None else baseline_git_sha,
            )

            if baseline_git_sha is None:
                baseline = build_initial_baseline(current)
            else:
                with self.repository.materialize_desired(baseline_git_sha) as baseline_desired:
                    baseline = validate_environment(
                        self.root,
                        options.environment,
                        desired_root=baseline_desired,
                    )
            plan = build_impact_plan(
                current,
                baseline,
                source_git_sha=source_git_sha,
                baseline_git_sha=baseline_git_sha,
                initial_deployment=baseline_git_sha is None,
            )
            self._complete_step(record, record_path, "build_impact_plan", f"{len(plan.changes)} change(s)")

            reports = [ManualProvisioningAdapter().preflight(item) for item in current.instances]
            failed_instances = [report.instance_id for report in reports if not report.passed]
            if failed_instances:
                raise DeploymentError(
                    f"manual provisioning preflight failed for: {', '.join(sorted(failed_instances))}"
                )
            record["provisioning"] = [report.to_dict() for report in reports]
            self._complete_step(record, record_path, "manual_provisioning_preflight", "passed")

            manifest_payload, manifest_request = self._prepare_backend_manifest(
                current=current,
                plan=plan,
                options=options,
                deployment_id=deployment_id,
                source_git_sha=source_git_sha,
                record=record,
                record_path=record_path,
                revision_state_path=revision_state_path,
            )

            files = render_files(current)
            files["backend-manifest.json"] = manifest_payload
            write_rendered_files(build_directory, files)
            validate_ansible_artifacts(build_directory, options.environment)
            impact_path = build_directory / "impact-plan.json"
            impact_path.write_bytes(plan.to_json_bytes())
            self._complete_step(record, record_path, "render_working_artifacts", str(build_directory))

            if options.apply:
                self._require_apply_inputs(options)
                bootstrap_targets = plan.affected["provision"]
                configure_targets = tuple(
                    sorted(
                        set(plan.affected["configure"])
                        | set(plan.affected["node_runtime"])
                        | set(bootstrap_targets)
                    )
                )
                # A node is bootstrapped once in its life. The impact plan calls
                # an instance "provision" whenever it is absent from the Git
                # baseline, which is also true of every already-running node on
                # any deployment that has no baseline yet — and the bootstrap
                # inventory reaches nodes on port 22, the one port a hardened
                # node no longer answers on. Left unfiltered, a node drops out
                # of the fleet precisely because the previous deployment to it
                # succeeded.
                #
                # Filtering happens after configure_targets is computed on
                # purpose: skipping the bootstrap of a live node must never also
                # skip its configuration.
                pending_bootstrap = tuple(
                    instance_id
                    for instance_id in bootstrap_targets
                    if not self._bootstrap_marker(
                        bootstrapped_directory, options.environment, instance_id
                    ).exists()
                )
                # Machine identity is a two-phase handshake: the node keeps its
                # private key, so the CA can only sign a CSR it is handed. The
                # bootstrap step is split accordingly and each phase is recorded
                # separately, so a resume does not re-collect signed requests.
                pki_directory = self._agent_pki_directory(record_path, deployment_id)
                self._reconcile_ansible_step(
                    record=record,
                    record_path=record_path,
                    step="bootstrap_csr",
                    targets=pending_bootstrap,
                    inventory=build_directory / "bootstrap-inventory.json",
                    playbook=self.root / "playbooks" / "bootstrap" / "csr.yml",
                    variables=options.bootstrap_vars,
                    extra_variables={
                        "pki_agent_csr_collect_directory": str(pki_directory / "csr"),
                    },
                )
                chain_variables = self._reconcile_agent_certificates(
                    record=record,
                    record_path=record_path,
                    targets=pending_bootstrap,
                    current=current,
                    options=options,
                    pki_directory=pki_directory,
                )
                self._reconcile_ansible_step(
                    record=record,
                    record_path=record_path,
                    step="bootstrap",
                    targets=pending_bootstrap,
                    inventory=build_directory / "bootstrap-inventory.json",
                    playbook=self.root / "playbooks" / "bootstrap" / "bootstrap.yml",
                    variables=options.bootstrap_vars,
                    extra_files=(chain_variables,) if chain_variables else (),
                )
                # Only once the step above has returned: a bootstrap that failed
                # raises, and an instance must never be recorded as bootstrapped
                # on the strength of an attempt.
                for instance_id in pending_bootstrap:
                    self._record_bootstrapped(
                        bootstrapped_directory,
                        options.environment,
                        instance_id,
                        source_git_sha=source_git_sha,
                    )
                self._reconcile_ansible_step(
                    record=record,
                    record_path=record_path,
                    step="configure",
                    targets=configure_targets,
                    inventory=build_directory / "ansible-inventory.json",
                    playbook=self.root / "playbooks" / "deploy" / "configure.yml",
                    variables=options.compiled_secrets,
                )
                self._reconcile_ansible_step(
                    record=record,
                    record_path=record_path,
                    step="readiness_gates",
                    targets=configure_targets,
                    inventory=build_directory / "ansible-inventory.json",
                    playbook=self.root / "playbooks" / "operations" / "readiness.yml",
                    variables=options.readiness_vars,
                )
            else:
                for step in (
                    "bootstrap_csr",
                    "sign_agent_certificates",
                    "bootstrap",
                    "configure",
                    "readiness_gates",
                ):
                    self._skip_step(record, record_path, step, "SKIPPED_DRY_RUN; no SSH or mutation")

            write_rendered_files(build_directory, files)
            impact_path = build_directory / "impact-plan.json"
            impact_path.write_bytes(plan.to_json_bytes())
            self._complete_step(record, record_path, "render_final_artifacts", str(build_directory))
            self._deliver_backend_manifest(
                record=record,
                record_path=record_path,
                current=current,
                options=options,
                request=manifest_request,
                identity_directory=backend_identity_directory,
            )
            record["updated_at"] = _timestamp()
            self._write_record(record_path, record)
            return record
        except Exception as exc:
            record["status"] = "FAILED"
            record["diagnostic"] = f"{type(exc).__name__}: {exc}"
            record["updated_at"] = _timestamp()
            self._write_record(record_path, record)
            if isinstance(exc, DeploymentError):
                raise
            raise DeploymentError(str(exc)) from exc

    def _prepare_backend_manifest(
        self,
        *,
        current: DesiredState,
        plan: ImpactPlan,
        options: DeploymentOptions,
        deployment_id: str,
        source_git_sha: str,
        record: dict[str, Any],
        record_path: Path,
        revision_state_path: Path,
    ) -> tuple[bytes, dict[str, Any]]:
        provisional = compile_backend_manifest(
            current,
            plan,
            revision=1,
            allow_destructive=options.allow_destructive,
        )
        payload_digest = backend_manifest_payload_digest(provisional)
        pinned = record.get("backend_manifest")
        if pinned is not None and not isinstance(pinned, dict):
            raise DeploymentError("deployment record contains malformed backend manifest metadata")
        allocation = ManifestRevisionAllocator(
            revision_state_path,
            options.environment,
        ).allocate(
            deployment_id=deployment_id,
            source_git_sha=source_git_sha,
            payload_digest=payload_digest,
            allow_destructive=options.allow_destructive,
            require_existing_allocation=pinned is not None,
        )
        request = compile_backend_manifest(
            current,
            plan,
            revision=allocation.revision,
            allow_destructive=options.allow_destructive,
        )
        rendered = backend_manifest_bytes(request)
        metadata = {
            "schema_version": 1,
            "artifact": "backend-manifest.json",
            "revision": allocation.revision,
            "allow_destructive": options.allow_destructive,
            "payload_digest": payload_digest,
            "rendered_sha256": f"sha256:{hashlib.sha256(rendered).hexdigest()}",
            "size_bytes": len(rendered),
        }
        if pinned is not None and pinned != metadata:
            raise DeploymentError(
                "resume backend manifest differs from the revision pinned in the deployment record"
            )
        record["backend_manifest"] = metadata
        # NOT_SENT is where every deployment starts; APPLIED is where it lands
        # once the manifest has been handed over. Anything else was written by
        # something this coordinator does not understand, and resuming on top of
        # it would guess at what the backend has already accepted.
        existing_apply = record.get("backend_apply")
        if existing_apply is None:
            record["backend_apply"] = {
                "status": "NOT_SENT",
                "applied_revision": None,
                "result": None,
            }
        elif (
            not isinstance(existing_apply, dict)
            or existing_apply.get("status") not in {"NOT_SENT", "APPLIED"}
        ):
            raise DeploymentError(
                "deployment record contains backend apply state unsupported by this coordinator"
            )
        self._write_record(record_path, record)
        self._complete_step(
            record,
            record_path,
            "allocate_backend_manifest_revision",
            f"revision {allocation.revision} allocated",
        )
        return rendered, request

    def _deliver_backend_manifest(
        self,
        *,
        record: dict[str, Any],
        record_path: Path,
        current: DesiredState,
        options: DeploymentOptions,
        request: dict[str, Any],
        identity_directory: Path,
    ) -> None:
        """Hand the compiled snapshot to the backend, or stop at the boundary."""

        supplied = (
            options.backend_client_certificate,
            options.backend_client_private_key,
            options.backend_certificate_authority,
        )
        # An explicitly supplied identity wins: an operator pointing at specific
        # files is making a deliberate choice, and quietly issuing a different
        # certificate underneath them would be the wrong kind of helpful.
        material: tuple[Path, Path, Path] | None
        if all(path is not None for path in supplied):
            material = supplied  # type: ignore[assignment]
        elif options.apply:
            material = self._ensure_backend_identity(
                current=current, options=options, directory=identity_directory
            )
        else:
            material = None
        # Sending is a mutation and needs an identity. Without either, the
        # workflow ends exactly where it always did: compiled, durably
        # allocated, and explicit that nothing was sent.
        if not options.apply or material is None:
            record["status"] = "WAITING_FOR_BACKEND"
            record["diagnostic"] = (
                "Infrastructure-only workflow reached the backend/agent contract boundary. "
                "The backend manifest revision is durably allocated and rendered, but no RPC was sent. "
                "Backend apply, DNS/data-plane promotion, and deployment-ref update were not performed."
            )
            return

        revision = record["backend_manifest"]["revision"]
        applied = record.get("backend_apply", {})
        if applied.get("status") == "APPLIED" and applied.get("applied_revision") == revision:
            # A resume must not re-send: the revision is already accepted, and
            # asking again would be a second chance to be told something new
            # about a decision the backend has already made.
            record["status"] = "BACKEND_APPLIED"
            record["diagnostic"] = (
                f"Backend manifest revision {revision} was already applied in an earlier attempt "
                f"({applied.get('result')}); nothing re-sent."
            )
            return

        host, port = current.environment.backend_endpoint.rsplit(":", 1)
        endpoint = BackendEndpoint(
            # The backend publishes its service port on the management overlay
            # only, so that is where it is reached; the certificate is issued
            # for the declared name, which has no DNS.
            target=f"{control_management_address(current.environment)}:{port}",
            tls_server_name=host,
            client_certificate=material[0],
            client_private_key=material[1],
            certificate_authority=material[2],
        )
        try:
            result = apply_fleet_manifest(
                request,
                endpoint=endpoint,
                timeout_seconds=options.backend_timeout_seconds,
            )
        except BackendCallError as exc:
            raise DeploymentError(str(exc)) from exc

        record["backend_apply"] = {
            "status": "APPLIED",
            "applied_revision": revision,
            "result": result,
        }
        record["status"] = "BACKEND_APPLIED"
        record["diagnostic"] = (
            f"Backend accepted manifest revision {revision} ({result}). "
            "DNS/data-plane promotion and the deployment-ref update remain separate, "
            "deliberately manual steps."
        )
        self._complete_step(
            record,
            record_path,
            "apply_backend_manifest",
            f"revision {revision}: {result}",
        )

    @staticmethod
    def _bootstrap_marker(bootstrapped_directory: Path, environment: str, instance_id: str) -> Path:
        return bootstrapped_directory / environment / f"{instance_id}.json"

    @classmethod
    def _record_bootstrapped(
        cls,
        bootstrapped_directory: Path,
        environment: str,
        instance_id: str,
        *,
        source_git_sha: str,
    ) -> None:
        marker = cls._bootstrap_marker(bootstrapped_directory, environment, instance_id)
        marker.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(marker.parent, 0o700)
        marker.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "instance_id": instance_id,
                    "environment": environment,
                    "source_git_sha": source_git_sha,
                    "bootstrapped_at": _timestamp(),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    # Порог обновления. Сертификат manifest-writer живёт на исполнителе и
    # обновляется выкаткой; недели хватает, чтобы протухание попало в обычный
    # прогон, а не в аварийный — сроков сертификатов у нас не мониторит никто.
    BACKEND_IDENTITY_RENEW_BEFORE_SECONDS = 7 * 24 * 3600

    def _ensure_backend_identity(
        self,
        *,
        current: DesiredState,
        options: DeploymentOptions,
        directory: Path,
    ) -> tuple[Path, Path, Path] | None:
        """Issue or renew the manifest-writer identity the coordinator itself uses.

        The CA root is an operator input because it is a root. This is a leaf,
        and the coordinator already holds the CA while it signs agent
        certificates — asking an operator to mint it by hand only adds a step
        that is forgotten once and then expires unnoticed.
        """
        if options.ca_state is None:
            return None
        environment = current.environment.object_id
        output = directory / environment
        certificate = output / "manifest-writer.crt"
        private_key = output / "manifest-writer.key"
        authority = output / "ca.crt"

        usable = certificate.is_file() and private_key.is_file() and authority.is_file()
        if usable and self._certificate_outlives(
            certificate, self.BACKEND_IDENTITY_RENEW_BEFORE_SECONDS
        ):
            return certificate, private_key, authority

        try:
            issue_control_certificate(
                current,
                "manifest-writer",
                ca_state=options.ca_state,
                output=output,
            )
        except PkiError as exc:
            raise DeploymentError(f"cannot issue the manifest-writer identity: {exc}") from exc
        return certificate, private_key, authority

    @staticmethod
    def _certificate_outlives(certificate: Path, seconds: int) -> bool:
        # openssl, not a parsed date: the PKI here shells out everywhere else,
        # and -checkend answers exactly the question being asked.
        result = subprocess.run(
            ["openssl", "x509", "-in", str(certificate), "-noout", "-checkend", str(seconds)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0

    @staticmethod
    def _prepare_state_directories(state_directory: Path) -> tuple[Path, Path, Path]:
        if state_directory.is_symlink():
            raise DeploymentError(f"refusing symlink deployment state root: {state_directory}")
        state_directory.mkdir(parents=True, exist_ok=True)
        os.chmod(state_directory, 0o700)
        record_directory = state_directory / "deployment-records"
        revision_directory = state_directory / "manifest-revisions"
        # Whether a machine has been bootstrapped is a property of the machine,
        # not of a Git diff, and it outlives any single deployment record.
        bootstrapped_directory = state_directory / "bootstrapped"
        if record_directory.is_symlink():
            raise DeploymentError(f"refusing symlink deployment record directory: {record_directory}")
        if revision_directory.is_symlink():
            raise DeploymentError(
                f"refusing symlink manifest revision directory: {revision_directory}"
            )
        if bootstrapped_directory.is_symlink():
            raise DeploymentError(
                f"refusing symlink bootstrap marker directory: {bootstrapped_directory}"
            )
        record_directory.mkdir(parents=True, exist_ok=True)
        revision_directory.mkdir(parents=True, exist_ok=True)
        bootstrapped_directory.mkdir(parents=True, exist_ok=True)
        os.chmod(record_directory, 0o700)
        os.chmod(revision_directory, 0o700)
        os.chmod(bootstrapped_directory, 0o700)
        return record_directory, revision_directory, bootstrapped_directory

    @staticmethod
    def _require_apply_inputs(options: DeploymentOptions) -> None:
        required = {
            "bootstrap_vars": options.bootstrap_vars,
            "compiled_secrets": options.compiled_secrets,
            "readiness_vars": options.readiness_vars,
        }
        missing = [name for name, path in required.items() if path is None or not path.is_file()]
        if missing:
            raise DeploymentError(f"--apply requires readable {', '.join(missing)} files")

    @staticmethod
    def _agent_pki_directory(record_path: Path, deployment_id: str) -> Path:
        # Beside the deployment record rather than inside the build directory:
        # render_files replaces that directory wholesale, which would delete the
        # collected requests between the two bootstrap phases.
        directory = record_path.parent.parent / "agent-pki" / deployment_id
        if directory.is_symlink():
            raise DeploymentError(f"refusing symlink agent PKI directory: {directory}")
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        return directory

    def _reconcile_agent_certificates(
        self,
        *,
        record: dict[str, Any],
        record_path: Path,
        targets: tuple[str, ...],
        current: DesiredState,
        options: DeploymentOptions,
        pki_directory: Path,
    ) -> Path | None:
        """Sign every collected CSR and return the chain variables file."""
        chain_path = pki_directory / "agent-chains.json"
        if self._step_is_completed(record, "sign_agent_certificates"):
            return chain_path if chain_path.is_file() else None
        if not targets:
            self._complete_step(
                record,
                record_path,
                "sign_agent_certificates",
                "no bootstrapped nodes; nothing to sign",
            )
            return None
        if options.ca_state is None:
            raise DeploymentError(
                "bootstrapping nodes requires --ca-state; the agent CSRs cannot be signed without it"
            )
        chains = self._sign_agent_certificates(
            targets=targets,
            current=current,
            ca_state=options.ca_state,
            pki_directory=pki_directory,
        )
        missing = [instance for instance in targets if instance not in chains]
        if missing:
            raise DeploymentError(
                "the CSR phase collected no request for: " + ", ".join(sorted(missing))
            )
        payload = json.dumps(
            {"spiritvpn_agent_certificate_chains": chains},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        descriptor = os.open(chain_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
        self._complete_step(
            record,
            record_path,
            "sign_agent_certificates",
            f"signed {len(chains)} machine certificate(s): {', '.join(sorted(chains))}",
        )
        return chain_path

    @staticmethod
    def _sign_agent_certificates(
        *,
        targets: tuple[str, ...],
        current: DesiredState,
        ca_state: Path,
        pki_directory: Path,
    ) -> dict[str, str]:
        csr_directory = pki_directory / "csr"
        signed_directory = pki_directory / "signed"
        chains: dict[str, str] = {}
        for instance_id in sorted(targets):
            csr_path = csr_directory / f"{instance_id}.csr"
            if csr_path.is_symlink() or not csr_path.is_file():
                continue
            try:
                sign_agent_certificate(
                    current,
                    instance_id,
                    ca_state=ca_state,
                    csr_pem=csr_path.read_bytes(),
                    output=signed_directory,
                )
            except PkiError as exc:
                raise DeploymentError(f"cannot sign the CSR for {instance_id}: {exc}") from exc
            chains[instance_id] = (signed_directory / f"{instance_id}-chain.pem").read_text(
                encoding="utf-8"
            )
        return chains

    def _reconcile_ansible_step(
        self,
        *,
        record: dict[str, Any],
        record_path: Path,
        step: str,
        targets: tuple[str, ...],
        inventory: Path,
        playbook: Path,
        variables: Path | None,
        extra_files: tuple[Path, ...] = (),
        extra_variables: dict[str, str] | None = None,
    ) -> None:
        if self._step_is_completed(record, step):
            return
        if not targets:
            self._complete_step(record, record_path, step, "no affected nodes; no Ansible invocation")
            return
        self._run_ansible(
            inventory,
            playbook,
            variables,
            limit=targets,
            extra_files=extra_files,
            extra_variables=extra_variables,
        )
        self._complete_step(
            record,
            record_path,
            step,
            f"Ansible converged {len(targets)} affected node(s): {', '.join(targets)}",
        )

    def _run_ansible(
        self,
        inventory: Path,
        playbook: Path,
        variables: Path | None,
        *,
        limit: tuple[str, ...],
        extra_files: tuple[Path, ...] = (),
        extra_variables: dict[str, str] | None = None,
    ) -> None:
        if not limit:
            raise DeploymentError("internal error: refusing an unbounded Ansible invocation")
        arguments = ["ansible-playbook", "-i", str(inventory), str(playbook)]
        arguments.extend(("--limit", ",".join(limit)))
        if variables is not None:
            arguments.extend(("--extra-vars", f"@{variables}"))
        for path in extra_files:
            arguments.extend(("--extra-vars", f"@{path}"))
        if extra_variables:
            arguments.extend(
                ("--extra-vars", json.dumps(extra_variables, ensure_ascii=False, sort_keys=True))
            )
        try:
            result = subprocess.run(arguments, cwd=self.root, check=False)
        except OSError as exc:
            raise DeploymentError(f"cannot execute ansible-playbook: {exc}") from exc
        if result.returncode != 0:
            raise DeploymentError(f"Ansible failed closed for {playbook.name}")

    @staticmethod
    def _complete_step(record: dict[str, Any], path: Path, name: str, diagnostic: str) -> None:
        DeploymentCoordinator._set_step(record, name, "COMPLETED", diagnostic)
        DeploymentCoordinator._write_record(path, record)

    @staticmethod
    def _step_is_completed(record: dict[str, Any], name: str) -> bool:
        return any(
            step.get("name") == name and step.get("status") == "COMPLETED"
            for step in record["steps"]
        )

    @staticmethod
    def _skip_step(record: dict[str, Any], path: Path, name: str, diagnostic: str) -> None:
        DeploymentCoordinator._set_step(record, name, "SKIPPED_DRY_RUN", diagnostic)
        DeploymentCoordinator._write_record(path, record)

    @staticmethod
    def _set_step(record: dict[str, Any], name: str, status: str, diagnostic: str) -> None:
        steps = record["steps"]
        entry = {
            "name": name,
            "status": status,
            "diagnostic": diagnostic,
            "timestamp": _timestamp(),
        }
        for index, step in enumerate(steps):
            if step["name"] == name:
                if step["status"] == "COMPLETED":
                    return
                steps[index] = entry
                break
        else:
            steps.append(entry)
        record["updated_at"] = entry["timestamp"]

    @staticmethod
    def _load_record(path: Path) -> dict[str, Any] | None:
        if path.is_symlink():
            raise DeploymentError(f"refusing symlink deployment record: {path}")
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DeploymentError(f"deployment record is unreadable: {path}: {exc}") from exc
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise DeploymentError(f"deployment record has an unsupported schema: {path}")
        return value

    @staticmethod
    def _write_record(path: Path, record: dict[str, Any]) -> None:
        payload = (json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
            os.chmod(path, 0o600)
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            if temporary.exists():
                temporary.unlink()
            raise


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
