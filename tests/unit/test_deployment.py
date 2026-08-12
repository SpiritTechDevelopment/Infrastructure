from __future__ import annotations

import fcntl
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

        self.assertEqual(record["status"], "WAITING_FOR_BACKEND")
        self.assertTrue(record["dry_run"])
        self.assertFalse(record["deployment_ref_updated"])
        self.assertEqual(record["source_git_sha"], source)
        self.assertEqual(ref, "")
        self.assertEqual(statuses["bootstrap"], "SKIPPED_DRY_RUN")
        self.assertEqual(statuses["configure"], "SKIPPED_DRY_RUN")
        self.assertEqual(statuses["readiness_gates"], "SKIPPED_DRY_RUN")

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

        self.assertEqual(resumed["deployment_id"], first["deployment_id"])
        self.assertEqual(resumed["status"], "WAITING_FOR_BACKEND")
        names = [step["name"] for step in resumed["steps"]]
        self.assertEqual(len(names), len(set(names)))

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
            record_directory = repository.root / "build" / "deployment-records"
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
