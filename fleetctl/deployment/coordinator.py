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

from fleetctl.adapters import GitRepository, validate_ansible_artifacts, write_rendered_files
from fleetctl.compiler import (
    backend_manifest_bytes,
    backend_manifest_payload_digest,
    compile_backend_manifest,
    render_files,
)
from fleetctl.model import DesiredState
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
        record_directory, revision_directory = self._prepare_state_directories(
            state_directory
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

            manifest_payload = self._prepare_backend_manifest(
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
                self._run_ansible(
                    build_directory / "bootstrap-inventory.json",
                    self.root / "playbooks" / "bootstrap" / "bootstrap.yml",
                    options.bootstrap_vars,
                )
                self._complete_step(record, record_path, "bootstrap", "Ansible completed")
                self._run_ansible(
                    build_directory / "ansible-inventory.json",
                    self.root / "playbooks" / "deploy" / "configure.yml",
                    options.compiled_secrets,
                )
                self._complete_step(record, record_path, "configure", "Ansible completed")
                self._run_ansible(
                    build_directory / "ansible-inventory.json",
                    self.root / "playbooks" / "operations" / "readiness.yml",
                    options.readiness_vars,
                )
                self._complete_step(record, record_path, "readiness_gates", "all gates passed")
            else:
                for step in ("bootstrap", "configure", "readiness_gates"):
                    self._skip_step(record, record_path, step, "SKIPPED_DRY_RUN; no SSH or mutation")

            write_rendered_files(build_directory, files)
            impact_path = build_directory / "impact-plan.json"
            impact_path.write_bytes(plan.to_json_bytes())
            self._complete_step(record, record_path, "render_final_artifacts", str(build_directory))
            record["status"] = "WAITING_FOR_BACKEND"
            record["diagnostic"] = (
                "Infrastructure-only workflow reached the backend/agent contract boundary. "
                "The backend manifest revision is durably allocated and rendered, but no RPC was sent. "
                "Backend apply, DNS/data-plane promotion, and deployment-ref update were not performed."
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
    ) -> bytes:
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
        backend_apply = {
            "status": "NOT_SENT",
            "applied_revision": None,
            "result": None,
        }
        if record.get("backend_apply", backend_apply) != backend_apply:
            raise DeploymentError(
                "deployment record contains backend apply state unsupported by the offline coordinator"
            )
        record["backend_apply"] = backend_apply
        self._write_record(record_path, record)
        self._complete_step(
            record,
            record_path,
            "allocate_backend_manifest_revision",
            f"revision {allocation.revision}; no backend RPC",
        )
        return rendered

    @staticmethod
    def _prepare_state_directories(state_directory: Path) -> tuple[Path, Path]:
        if state_directory.is_symlink():
            raise DeploymentError(f"refusing symlink deployment state root: {state_directory}")
        state_directory.mkdir(parents=True, exist_ok=True)
        os.chmod(state_directory, 0o700)
        record_directory = state_directory / "deployment-records"
        revision_directory = state_directory / "manifest-revisions"
        if record_directory.is_symlink():
            raise DeploymentError(f"refusing symlink deployment record directory: {record_directory}")
        if revision_directory.is_symlink():
            raise DeploymentError(
                f"refusing symlink manifest revision directory: {revision_directory}"
            )
        record_directory.mkdir(parents=True, exist_ok=True)
        revision_directory.mkdir(parents=True, exist_ok=True)
        os.chmod(record_directory, 0o700)
        os.chmod(revision_directory, 0o700)
        return record_directory, revision_directory

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

    def _run_ansible(self, inventory: Path, playbook: Path, variables: Path | None) -> None:
        arguments = ["ansible-playbook", "-i", str(inventory), str(playbook)]
        if variables is not None:
            arguments.extend(("--extra-vars", f"@{variables}"))
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
