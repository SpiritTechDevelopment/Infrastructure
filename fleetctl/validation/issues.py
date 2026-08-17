"""Stable validation diagnostics suitable for CLI and tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, order=True, slots=True)
class ValidationIssue:
    path: str
    code: str
    message: str

    @classmethod
    def at(cls, path: Path | str, code: str, message: str) -> "ValidationIssue":
        return cls(str(path), code, message)

    def render(self) -> str:
        return f"{self.path}: [{self.code}] {self.message}"


class DesiredStateInvalid(Exception):
    def __init__(self, issues: list[ValidationIssue]):
        self.issues = sorted(issues)
        super().__init__(f"desired state has {len(self.issues)} validation error(s)")
