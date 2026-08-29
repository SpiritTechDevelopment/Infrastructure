from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Свой каталог на пути явно: тесты запускаются и через `unittest discover`,
# и как `tests.unit.<модуль>`, и во втором случае соседний модуль иначе не
# находится.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import topology_fixture

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
        # Auto-maintenance forks a process that keeps writing into .git/objects
        # after the committing command has returned. A test that finishes soon
        # after its last commit then races that process while removing its
        # temporary directory, and rmtree fails with ENOTEMPTY. The fixture is
        # short-lived and never needs packing, so the cheapest fix is to leave
        # no background writer behind at all.
        self.git("config", "gc.auto", "0")
        self.git("config", "maintenance.auto", "false")
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
        topology_fixture.edit(
            self.root / "desired",
            "develop-entry-nl-01",
            lambda document: document["spec"].__setitem__("public_address", address),
        )
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

    def run_manifest(self, root: Path, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = main(
                [
                    "--root",
                    str(root),
                    "manifest",
                    "--environment",
                    "develop",
                    *arguments,
                ]
            )
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def run_check_change(
        self,
        root: Path,
        output: Path,
        *arguments: str,
    ) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = main(
                [
                    "--root",
                    str(root),
                    "check-change",
                    "--environment",
                    "develop",
                    "--output",
                    str(output),
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

    def test_manifest_cli_renders_full_initial_snapshot_without_rpc(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.make_repository(Path(temporary))
            exit_code, stdout, stderr = self.run_manifest(
                repository.root,
                "--initial",
                "--revision",
                "42",
            )
            manifest = json.loads(stdout)

        self.assertEqual((exit_code, stderr), (0, ""))
        self.assertEqual(manifest["revision"], 42)
        self.assertFalse(manifest["allow_destructive"])
        self.assertEqual(len(manifest["nodes"]), 2)
        self.assertEqual(len(manifest["fleets"]), 1)

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

    def test_check_change_compiles_the_transition_from_the_deployment_ref(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.make_repository(Path(temporary))
            baseline = repository.head()
            repository.git("update-ref", "refs/deployments/develop", baseline)
            source = repository.change_entry_address_and_commit()
            output = repository.root / "build" / "develop"

            exit_code, stdout, stderr = self.run_check_change(repository.root, output)
            summary = json.loads(stdout)
            impact = json.loads((output / "impact-plan.json").read_text(encoding="utf-8"))
            inventory_exists = (output / "ansible-inventory.json").is_file()

        self.assertEqual((exit_code, stderr), (0, ""))
        self.assertEqual(summary["source_git_sha"], source)
        self.assertEqual(summary["baseline_git_sha"], baseline)
        self.assertEqual(summary["transition_kinds"], ["modification"])
        self.assertGreater(summary["compiled_host_count"], 0)
        self.assertIn("INSTANCE_CHANGED", {item["type"] for item in impact["changes"]})
        self.assertTrue(inventory_exists)

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
