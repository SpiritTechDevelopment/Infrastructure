"""Fail-closed local Git access for deployment baselines."""

from __future__ import annotations

import subprocess
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Iterator


DEPLOYMENT_REF_PREFIX = "refs/deployments"


class GitAdapterError(Exception):
    """Raised when Git cannot prove the requested deployment state."""


class GitRepository:
    def __init__(self, root: Path):
        self.root = root.resolve()
        probe = self._run("rev-parse", "--show-toplevel", check=False)
        if probe.returncode != 0:
            raise GitAdapterError(f"not a readable Git work tree: {self.root}")
        top_level = Path(probe.stdout.decode("utf-8").strip()).resolve()
        if top_level != self.root:
            raise GitAdapterError(
                f"repository root must be the Git top level: expected {top_level}, got {self.root}"
            )

    @staticmethod
    def deployment_ref(environment: str) -> str:
        if environment not in {"develop", "staging", "prod"}:
            raise GitAdapterError(f"unsupported environment: {environment!r}")
        return f"{DEPLOYMENT_REF_PREFIX}/{environment}"

    def resolve_commit(self, revision: str) -> str:
        if not revision:
            raise GitAdapterError("Git revision must not be empty")
        result = self._run("rev-parse", "--verify", f"{revision}^{{commit}}", check=False)
        if result.returncode != 0:
            raise GitAdapterError(f"Git revision is not a readable commit: {revision!r}")
        commit = result.stdout.decode("ascii").strip()
        if not commit or any(character not in "0123456789abcdef" for character in commit):
            raise GitAdapterError(f"Git returned an invalid object ID for {revision!r}")
        return commit

    def resolve_deployment_baseline(self, environment: str) -> str | None:
        ref = self.deployment_ref(environment)
        result = self._run(
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            ref,
            check=False,
        )
        if result.returncode != 0:
            raise GitAdapterError(f"deployment baseline ref is unreadable: {ref}")
        matching = [
            line.split(" ", 1)[1]
            for line in result.stdout.decode("ascii").splitlines()
            if line.startswith(f"{ref} ")
        ]
        if not matching:
            return None
        if len(matching) != 1:
            raise GitAdapterError(f"deployment baseline ref is ambiguous: {ref}")
        return self.resolve_commit(ref)

    def assert_desired_matches_commit(self, commit: str) -> None:
        commit = self.resolve_commit(commit)
        difference = self._run(
            "diff",
            "--quiet",
            "--no-ext-diff",
            commit,
            "--",
            "desired",
            check=False,
        )
        if difference.returncode not in {0, 1}:
            raise GitAdapterError(f"cannot compare desired/ with source commit {commit}")
        untracked = self._run(
            "ls-files",
            "--others",
            "--exclude-standard",
            "--",
            "desired",
        ).stdout.decode("utf-8").splitlines()
        if difference.returncode == 1 or untracked:
            detail = f"; untracked: {', '.join(sorted(untracked))}" if untracked else ""
            raise GitAdapterError(
                f"desired/ differs from source commit {commit}; refusing to attach a false source_git_sha{detail}"
            )

    @contextmanager
    def materialize_desired(self, commit: str) -> Iterator[Path]:
        commit = self.resolve_commit(commit)
        archive = self._run("archive", "--format=tar", commit, "desired", check=False)
        if archive.returncode != 0:
            raise GitAdapterError(f"desired/ is missing or unreadable in commit {commit}")
        with tempfile.TemporaryDirectory(prefix="fleetctl-git-desired-") as temporary:
            destination = Path(temporary)
            archive_path = destination / "desired.tar"
            archive_path.write_bytes(archive.stdout)
            try:
                with tarfile.open(archive_path, mode="r:") as tar:
                    self._extract_desired_archive(tar, destination)
            except (tarfile.TarError, OSError) as exc:
                raise GitAdapterError(f"cannot read desired/ from commit {commit}: {exc}") from exc
            desired_root = destination / "desired"
            if not desired_root.is_dir():
                raise GitAdapterError(f"desired/ is missing from commit {commit}")
            yield desired_root

    def update_deployment_ref(
        self,
        environment: str,
        source_commit: str,
        *,
        expected_baseline: str | None,
    ) -> str:
        ref = self.deployment_ref(environment)
        source_commit = self.resolve_commit(source_commit)
        if expected_baseline is None:
            current = self.resolve_deployment_baseline(environment)
            if current is not None:
                raise GitAdapterError(
                    f"initial update refused: deployment baseline already exists at {current}"
                )
            zero_oid = "0" * len(source_commit)
            expected = zero_oid
        else:
            expected = self.resolve_commit(expected_baseline)

        result = self._run(
            "update-ref",
            "-m",
            f"fleetctl: record successful {environment} deployment",
            ref,
            source_commit,
            expected,
            check=False,
        )
        if result.returncode != 0:
            raise GitAdapterError(
                f"atomic update refused for {ref}: expected baseline {expected_baseline or '<missing>'}"
            )
        return source_commit

    @staticmethod
    def _extract_desired_archive(archive: tarfile.TarFile, destination: Path) -> None:
        for member in archive.getmembers():
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise GitAdapterError(f"unsafe path in Git archive: {member.name!r}")
            if relative.parts[0] != "desired":
                raise GitAdapterError(f"unexpected path in Git archive: {member.name!r}")
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise GitAdapterError(f"unsupported entry in Git archive: {member.name!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise GitAdapterError(f"cannot extract Git archive entry: {member.name!r}")
            target.write_bytes(source.read())

    def _run(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        try:
            result = subprocess.run(
                ["git", "-C", str(self.root), *arguments],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except OSError as exc:
            raise GitAdapterError(f"cannot execute Git: {exc}") from exc
        if check and result.returncode != 0:
            operation = arguments[0] if arguments else "command"
            raise GitAdapterError(f"Git {operation} failed in {self.root}")
        return result
