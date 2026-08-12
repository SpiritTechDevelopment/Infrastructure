from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from fleetctl.adapters import GitAdapterError, GitRepository
from fleetctl.cli import main


REPO_ROOT = Path(__file__).resolve().parents[2]
VALID_DESIRED = REPO_ROOT / "tests" / "fixtures" / "valid" / "desired"


class TemporaryFleetRepository:
    def __init__(self, root: Path):
        self.root = root
        shutil.copytree(VALID_DESIRED, root / "desired")
        shutil.copytree(REPO_ROOT / "contracts" / "desired-state", root / "contracts" / "desired-state")
        self.git("init", "-q")
        self.git("config", "user.name", "fleetctl test")
        self.git("config", "user.email", "fleetctl@example.invalid")
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")

    def git(self, *arguments: str, input_bytes: bytes | None = None) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.root), *arguments],
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return result.stdout.decode("ascii").strip()

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    def change_entry_address_and_commit(self, address: str = "192.0.2.11") -> str:
        path = (
            self.root
            / "desired"
            / "environments"
            / "develop"
            / "instances"
            / "develop-entry-nl-01.yml"
        )
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        document["spec"]["public_address"] = address
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        self.git("add", "desired")
        self.git("commit", "-qm", "change source")
        return self.head()


class GitDeploymentBaselineTests(unittest.TestCase):
    def make_repository(self, parent: Path) -> TemporaryFleetRepository:
        root = parent / "repository"
        root.mkdir()
        return TemporaryFleetRepository(root)

    def run_plan(self, root: Path, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = main(
                [
                    "--root",
                    str(root),
                    "plan",
                    "--environment",
                    "develop",
                    *arguments,
                ]
            )
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_missing_baseline_is_fail_closed_unless_initial_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.make_repository(Path(temporary))
            exit_code, _, stderr = self.run_plan(repository.root)
            self.assertEqual(exit_code, 2)
            self.assertIn("deployment baseline", stderr)
            self.assertIn("missing", stderr)

            exit_code, stdout, _ = self.run_plan(repository.root, "--initial")
            self.assertEqual(exit_code, 0)
            plan = json.loads(stdout)
            source = repository.head()

        self.assertTrue(plan["initial_deployment"])
        self.assertIsNone(plan["baseline_git_sha"])
        self.assertEqual(plan["source_git_sha"], source)
        self.assertIn("INSTANCE_ADDED", {change["type"] for change in plan["changes"]})

    def test_plan_reads_both_commits_and_never_moves_deployment_ref(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.make_repository(Path(temporary))
            baseline = repository.head()
            repository.git("update-ref", "refs/deployments/develop", baseline)
            source = repository.change_entry_address_and_commit()

            exit_code, stdout, stderr = self.run_plan(repository.root)
            self.assertEqual((exit_code, stderr), (0, ""))
            plan = json.loads(stdout)
            ref_after_plan = repository.git("rev-parse", "refs/deployments/develop")

        self.assertEqual(plan["source_git_sha"], source)
        self.assertEqual(plan["baseline_git_sha"], baseline)
        self.assertFalse(plan["initial_deployment"])
        self.assertEqual(ref_after_plan, baseline)
        self.assertIn("INSTANCE_CHANGED", {change["type"] for change in plan["changes"]})

    def test_materialized_desired_comes_from_commit_not_working_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.make_repository(Path(temporary))
            source = repository.head()
            path = repository.root / "desired" / "fleet-ids.yml"
            path.write_text("changed: 99\n", encoding="utf-8")
            adapter = GitRepository(repository.root)
            with adapter.materialize_desired(source) as desired_root:
                committed = (desired_root / "fleet-ids.yml").read_text(encoding="utf-8")

        self.assertEqual(committed, "develop-fleet-eu: 1\n")

    def test_dirty_or_untracked_desired_refuses_source_sha(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.make_repository(Path(temporary))
            source = repository.head()
            adapter = GitRepository(repository.root)
            tracked = repository.root / "desired" / "fleet-ids.yml"
            tracked.write_text("develop-fleet-eu: 2\n", encoding="utf-8")
            with self.assertRaises(GitAdapterError):
                adapter.assert_desired_matches_commit(source)

            repository.git("restore", "desired/fleet-ids.yml")
            untracked = repository.root / "desired" / "untracked.yml"
            untracked.write_text("unexpected: true\n", encoding="utf-8")
            with self.assertRaises(GitAdapterError):
                adapter.assert_desired_matches_commit(source)

    def test_non_commit_baseline_ref_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.make_repository(Path(temporary))
            blob = repository.git("hash-object", "-w", "--stdin", input_bytes=b"not a commit")
            repository.git("update-ref", "refs/deployments/develop", blob)
            with self.assertRaises(GitAdapterError):
                GitRepository(repository.root).resolve_deployment_baseline("develop")

    def test_atomic_update_ref_rejects_stale_or_incorrect_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.make_repository(Path(temporary))
            baseline = repository.head()
            repository.git("update-ref", "refs/deployments/develop", baseline)
            source = repository.change_entry_address_and_commit()
            adapter = GitRepository(repository.root)

            with self.assertRaises(GitAdapterError):
                adapter.update_deployment_ref("develop", source, expected_baseline=source)
            self.assertEqual(repository.git("rev-parse", "refs/deployments/develop"), baseline)

            updated = adapter.update_deployment_ref(
                "develop",
                source,
                expected_baseline=baseline,
            )
            self.assertEqual(updated, source)
            self.assertEqual(repository.git("rev-parse", "refs/deployments/develop"), source)
            with self.assertRaises(GitAdapterError):
                adapter.update_deployment_ref("develop", source, expected_baseline=None)


if __name__ == "__main__":
    unittest.main()
