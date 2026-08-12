from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from fleetctl.deployment import DeploymentCoordinator, DeploymentError, DeploymentOptions
from tests.unit.test_git_adapter import TemporaryFleetRepository


class InfrastructureDeploymentCoordinatorTests(unittest.TestCase):
    def prepare_repository(self, parent: Path) -> TemporaryFleetRepository:
        root = parent / "repository"
        root.mkdir()
        repository = TemporaryFleetRepository(root)
        instances = repository.root / "desired" / "environments" / "develop" / "instances"
        for name, address in (
            ("develop-entry-nl-01.yml", "1.1.1.1"),
            ("develop-exit-de-01.yml", "8.8.8.8"),
        ):
            path = instances / name
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            document["spec"]["public_address"] = address
            path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        repository.git("add", "desired")
        repository.git("commit", "-qm", "use routable fixture addresses")
        return repository

    def test_dry_run_reaches_waiting_for_backend_without_external_commands_or_ref_update(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.prepare_repository(Path(temporary))
            source = repository.head()
            coordinator = DeploymentCoordinator(repository.root)
            with mock.patch.object(coordinator, "_run_ansible", side_effect=AssertionError("unexpected Ansible")):
                record = coordinator.run(DeploymentOptions(environment="develop", initial=True))

            ref = repository.git("for-each-ref", "--format=%(refname)", "refs/deployments/develop")
            statuses = {step["name"]: step["status"] for step in record["steps"]}
            manifest_path = repository.root / "build" / "develop" / "backend-manifest.json"
            manifest_bytes = manifest_path.read_bytes()
            revision_path = (
                repository.root
                / ".fleetctl-state"
                / "manifest-revisions"
                / "develop.json"
            )
            revision_state = json.loads(revision_path.read_text(encoding="utf-8"))
            revision_mode = oct(revision_path.stat().st_mode & 0o777)

        self.assertEqual(record["status"], "WAITING_FOR_BACKEND")
        self.assertTrue(record["dry_run"])
        self.assertFalse(record["deployment_ref_updated"])
        self.assertEqual(record["source_git_sha"], source)
        self.assertEqual(ref, "")
        self.assertEqual(statuses["bootstrap"], "SKIPPED_DRY_RUN")
        self.assertEqual(statuses["configure"], "SKIPPED_DRY_RUN")
        self.assertEqual(statuses["readiness_gates"], "SKIPPED_DRY_RUN")
        self.assertEqual(statuses["allocate_backend_manifest_revision"], "COMPLETED")
        self.assertEqual(record["backend_manifest"]["revision"], 1)
        self.assertEqual(
            record["backend_manifest"]["rendered_sha256"],
            f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}",
        )
        self.assertEqual(record["backend_manifest"]["size_bytes"], len(manifest_bytes))
        self.assertEqual(record["backend_apply"]["status"], "NOT_SENT")
        self.assertEqual(revision_state["last_allocated_revision"], 1)
        self.assertEqual(revision_mode, "0o600")

    def test_resume_is_explicit_and_preserves_completed_steps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.prepare_repository(Path(temporary))
            coordinator = DeploymentCoordinator(repository.root)
            options = DeploymentOptions(environment="develop", initial=True)
            first = coordinator.run(options)
            with self.assertRaises(DeploymentError):
                coordinator.run(options)
            resumed = coordinator.run(
                DeploymentOptions(environment="develop", initial=True, resume=True)
            )
            revision_path = (
                repository.root
                / ".fleetctl-state"
                / "manifest-revisions"
                / "develop.json"
            )
            revision_state = json.loads(revision_path.read_text(encoding="utf-8"))

        self.assertEqual(resumed["deployment_id"], first["deployment_id"])
        self.assertEqual(resumed["status"], "WAITING_FOR_BACKEND")
        self.assertEqual(resumed["backend_manifest"], first["backend_manifest"])
        self.assertEqual(revision_state["last_allocated_revision"], 1)
        self.assertEqual(len(revision_state["allocations"]), 1)
        names = [step["name"] for step in resumed["steps"]]
        self.assertEqual(len(names), len(set(names)))

    def test_new_deployment_gets_next_revision_in_same_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.prepare_repository(Path(temporary))
            coordinator = DeploymentCoordinator(repository.root)
            first = coordinator.run(DeploymentOptions(environment="develop", initial=True))
            node_path = (
                repository.root
                / "desired"
                / "environments"
                / "develop"
                / "nodes"
                / "develop-entry-nl.yml"
            )
            node = yaml.safe_load(node_path.read_text(encoding="utf-8"))
            node["spec"]["display_name"] = "Netherlands updated"
            node_path.write_text(yaml.safe_dump(node, sort_keys=False), encoding="utf-8")
            repository.git("add", "desired")
            repository.git("commit", "-qm", "change manifest payload")
            second = coordinator.run(DeploymentOptions(environment="develop", initial=True))

        self.assertEqual(first["backend_manifest"]["revision"], 1)
        self.assertEqual(second["backend_manifest"]["revision"], 2)
        self.assertNotEqual(
            first["backend_manifest"]["payload_digest"],
            second["backend_manifest"]["payload_digest"],
        )

    def test_destructive_manifest_requires_and_records_explicit_permission(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.prepare_repository(Path(temporary))
            baseline = repository.head()
            repository.git("update-ref", "refs/deployments/develop", baseline)
            fleet_path = (
                repository.root
                / "desired"
                / "environments"
                / "develop"
                / "fleets"
                / "develop-fleet-eu.yml"
            )
            fleet = yaml.safe_load(fleet_path.read_text(encoding="utf-8"))
            fleet["spec"]["entries"] = []
            fleet["spec"]["bridges"] = []
            fleet_path.write_text(yaml.safe_dump(fleet, sort_keys=False), encoding="utf-8")
            repository.git("add", "desired")
            repository.git("commit", "-qm", "begin two-phase node decommission")

            record = DeploymentCoordinator(repository.root).run(
                DeploymentOptions(environment="develop", allow_destructive=True)
            )

        self.assertTrue(record["backend_manifest"]["allow_destructive"])
        self.assertEqual(record["backend_manifest"]["revision"], 1)
        self.assertEqual(record["backend_apply"]["status"], "NOT_SENT")

    def test_resume_fails_closed_when_revision_state_is_lost(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.prepare_repository(Path(temporary))
            coordinator = DeploymentCoordinator(repository.root)
            coordinator.run(DeploymentOptions(environment="develop", initial=True))
            revision_path = (
                repository.root
                / ".fleetctl-state"
                / "manifest-revisions"
                / "develop.json"
            )
            revision_path.unlink()

            with self.assertRaisesRegex(DeploymentError, "revision state is missing"):
                coordinator.run(
                    DeploymentOptions(environment="develop", initial=True, resume=True)
                )

    def test_resume_fails_closed_when_pinned_manifest_metadata_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.prepare_repository(Path(temporary))
            coordinator = DeploymentCoordinator(repository.root)
            record = coordinator.run(DeploymentOptions(environment="develop", initial=True))
            record_path = (
                repository.root
                / ".fleetctl-state"
                / "deployment-records"
                / f"{record['deployment_id']}.json"
            )
            stored = json.loads(record_path.read_text(encoding="utf-8"))
            stored["backend_manifest"]["rendered_sha256"] = "sha256:" + "0" * 64
            record_path.write_text(json.dumps(stored), encoding="utf-8")
            os.chmod(record_path, 0o600)

            with self.assertRaisesRegex(DeploymentError, "differs from the revision pinned"):
                coordinator.run(
                    DeploymentOptions(environment="develop", initial=True, resume=True)
                )

    def test_explicit_apply_resume_cannot_keep_a_false_dry_run_label(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.prepare_repository(Path(temporary))
            coordinator = DeploymentCoordinator(repository.root)
            coordinator.run(DeploymentOptions(environment="develop", initial=True))
            variables = {}
            for name in ("bootstrap.yml", "secrets.yml", "readiness.yml"):
                path = repository.root / name
                path.write_text("{}\n", encoding="utf-8")
                variables[name] = path
            with mock.patch.object(coordinator, "_run_ansible"):
                resumed = coordinator.run(
                    DeploymentOptions(
                        environment="develop",
                        initial=True,
                        resume=True,
                        apply=True,
                        bootstrap_vars=variables["bootstrap.yml"],
                        compiled_secrets=variables["secrets.yml"],
                        readiness_vars=variables["readiness.yml"],
                    )
                )

        self.assertFalse(resumed["dry_run"])
        self.assertEqual(resumed["status"], "WAITING_FOR_BACKEND")

    def test_apply_requires_all_operator_inputs_before_ansible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.prepare_repository(Path(temporary))
            coordinator = DeploymentCoordinator(repository.root)
            with mock.patch.object(coordinator, "_run_ansible") as ansible:
                with self.assertRaises(DeploymentError):
                    coordinator.run(
                        DeploymentOptions(environment="develop", initial=True, apply=True)
                    )
                ansible.assert_not_called()

    def test_environment_lock_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.prepare_repository(Path(temporary))
            record_directory = repository.root / ".fleetctl-state" / "deployment-records"
            record_directory.mkdir(parents=True)
            lock_path = record_directory / "develop.lock"
            with lock_path.open("a+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(DeploymentError):
                    DeploymentCoordinator(repository.root).run(
                        DeploymentOptions(environment="develop", initial=True)
                    )


if __name__ == "__main__":
    unittest.main()
