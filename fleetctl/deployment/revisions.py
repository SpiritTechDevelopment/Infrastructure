"""Durable, environment-scoped backend manifest revision allocation."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MAX_UINT64 = 2**64 - 1


class RevisionStateError(ValueError):
    """Revision state is missing, corrupt, or conflicts with a deployment."""


@dataclass(frozen=True, slots=True)
class RevisionAllocation:
    revision: int
    deployment_id: str
    source_git_sha: str
    payload_digest: str
    allow_destructive: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "source_git_sha": self.source_git_sha,
            "payload_digest": self.payload_digest,
            "allow_destructive": self.allow_destructive,
        }


class ManifestRevisionAllocator:
    """Allocate one monotonic revision per deployment under an external env lock."""

    def __init__(self, path: Path, environment: str):
        self.path = path
        self.environment = environment

    def allocate(
        self,
        *,
        deployment_id: str,
        source_git_sha: str,
        payload_digest: str,
        allow_destructive: bool,
        require_existing_allocation: bool = False,
    ) -> RevisionAllocation:
        state = self._load(require_existing=require_existing_allocation)
        existing = state["allocations"].get(deployment_id)
        requested = {
            "source_git_sha": source_git_sha,
            "payload_digest": payload_digest,
            "allow_destructive": allow_destructive,
        }
        if existing is not None:
            actual = {key: existing[key] for key in requested}
            if actual != requested:
                raise RevisionStateError(
                    f"revision allocation for {deployment_id!r} conflicts with the current manifest"
                )
            return RevisionAllocation(
                revision=existing["revision"],
                deployment_id=deployment_id,
                **requested,
            )

        if require_existing_allocation:
            raise RevisionStateError(
                f"revision allocation is missing for pinned deployment {deployment_id!r}"
            )

        last_revision = state["last_allocated_revision"]
        if last_revision >= MAX_UINT64:
            raise RevisionStateError(
                f"manifest revision space is exhausted for environment {self.environment}"
            )
        allocation = RevisionAllocation(
            revision=last_revision + 1,
            deployment_id=deployment_id,
            **requested,
        )
        state["last_allocated_revision"] = allocation.revision
        state["allocations"][deployment_id] = allocation.to_dict()
        self._write(state)
        return allocation

    def _load(self, *, require_existing: bool) -> dict[str, Any]:
        if self.path.is_symlink():
            raise RevisionStateError(f"refusing symlink revision state: {self.path}")
        if not self.path.exists():
            if require_existing:
                raise RevisionStateError(
                    f"revision state is missing for a pinned deployment: {self.path}"
                )
            return {
                "schema_version": 1,
                "environment": self.environment,
                "last_allocated_revision": 0,
                "allocations": {},
            }
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RevisionStateError(f"revision state is unreadable: {self.path}: {exc}") from exc
        self._validate(state)
        return state

    def _validate(self, state: object) -> None:
        if not isinstance(state, dict) or set(state) != {
            "schema_version",
            "environment",
            "last_allocated_revision",
            "allocations",
        }:
            raise RevisionStateError(f"revision state has an unsupported schema: {self.path}")
        if state["schema_version"] != 1 or state["environment"] != self.environment:
            raise RevisionStateError(f"revision state belongs to another schema or environment: {self.path}")
        last = state["last_allocated_revision"]
        allocations = state["allocations"]
        if (
            not isinstance(last, int)
            or isinstance(last, bool)
            or not 0 <= last <= MAX_UINT64
            or not isinstance(allocations, dict)
        ):
            raise RevisionStateError(f"revision state contains invalid counters: {self.path}")
        seen_revisions: set[int] = set()
        for deployment_id, allocation in allocations.items():
            if not isinstance(deployment_id, str) or not deployment_id:
                raise RevisionStateError(f"revision state contains an invalid deployment ID: {self.path}")
            if not isinstance(allocation, dict) or set(allocation) != {
                "revision",
                "source_git_sha",
                "payload_digest",
                "allow_destructive",
            }:
                raise RevisionStateError(f"revision allocation is malformed for {deployment_id!r}")
            revision = allocation["revision"]
            if (
                not isinstance(revision, int)
                or isinstance(revision, bool)
                or not 1 <= revision <= last
                or revision in seen_revisions
                or not isinstance(allocation["source_git_sha"], str)
                or not allocation["source_git_sha"]
                or not isinstance(allocation["payload_digest"], str)
                or not allocation["payload_digest"].startswith("sha256:")
                or not isinstance(allocation["allow_destructive"], bool)
            ):
                raise RevisionStateError(f"revision allocation is invalid for {deployment_id!r}")
            seen_revisions.add(revision)

    def _write(self, state: dict[str, Any]) -> None:
        parent = self.path.parent
        if parent.is_symlink():
            raise RevisionStateError(f"refusing symlink revision directory: {parent}")
        parent.mkdir(parents=True, exist_ok=True)
        os.chmod(parent, 0o700)
        payload = (json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
            os.chmod(self.path, 0o600)
            directory_descriptor = os.open(parent, os.O_RDONLY)
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
