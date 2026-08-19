#!/usr/bin/env python3
"""Decide from the executor transcript whether the deployment ref may advance.

The hub prints the coordinator's deployment record as the last thing on stdout,
after everything Ansible had to say. That record is the only report of what the
deployment actually reached, and `fleet-deploy` exits 0 in more than one of those
outcomes: an apply run that never obtained a manifest-writer identity stops at
WAITING_FOR_BACKEND and still succeeds. Reading the exit code alone would promote
the ref for a deployment that never crossed the backend boundary.

Everything here is fail-closed and stdlib-only: it runs on the self-hosted runner
before any Python environment has been prepared.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


class RecordError(Exception):
    """The transcript does not describe the deployment that was requested."""


# Present in every record this coordinator writes, from the first line of
# `_run_locked` onwards. A document missing any of them was written by something
# else, and guessing at its meaning is exactly what must not happen here.
REQUIRED_KEYS = (
    "schema_version",
    "deployment_id",
    "environment",
    "source_git_sha",
    "baseline_git_sha",
    "dry_run",
    "status",
    "deployment_ref_updated",
)


def extract_record(transcript: str) -> dict[str, Any]:
    """Return the deployment record printed at the end of the transcript.

    `json.dumps(..., indent=2)` puts a bare `{` in column zero and nothing else
    on that line, so candidate starts are cheap to find. Each candidate must
    parse as one complete document running to the end of the transcript, which
    only the final record can do — Ansible output that happens to look like JSON
    is followed by more text and fails to parse.
    """

    lines = transcript.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if lines[index] != "{":
            continue
        try:
            candidate = json.loads("\n".join(lines[index:]))
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and all(key in candidate for key in REQUIRED_KEYS):
            return candidate
    raise RecordError("no deployment record found at the end of the executor transcript")


def require_identity(
    record: dict[str, Any],
    *,
    environment: str,
    source_git_sha: str,
    expected_baseline: str | None,
) -> None:
    """Refuse a record that describes some other deployment.

    A mismatch here is never a "do not promote" — it means the transcript and the
    request have come apart, and the run has to stop rather than quietly decide.
    """

    if record["schema_version"] != 1:
        raise RecordError(f"unsupported deployment record schema: {record['schema_version']!r}")
    expected_identity = {
        "deployment_id": f"{environment}-{source_git_sha[:12]}",
        "environment": environment,
        "source_git_sha": source_git_sha,
        "baseline_git_sha": expected_baseline,
    }
    for key, expected in expected_identity.items():
        if record[key] != expected:
            raise RecordError(
                f"deployment record {key} is {record[key]!r}, but the run requested {expected!r}"
            )
    # The coordinator never touches the ref; this workflow is the only thing that
    # does, and it does so after reading this record. True here means the record
    # was written by a coordinator whose contract is not the one assumed below.
    if record["deployment_ref_updated"] is not False:
        raise RecordError(
            "deployment record already claims the deployment ref was updated; refusing to promote"
        )


def promotion_refusal(record: dict[str, Any]) -> str | None:
    """Return why the ref must stay where it is, or None when it may advance."""

    if record["dry_run"] is not False:
        return "deployment ran as a dry run"
    if record["status"] != "BACKEND_APPLIED":
        diagnostic = record.get("diagnostic") or "no diagnostic recorded"
        return f"deployment status is {record['status']!r}, not BACKEND_APPLIED: {diagnostic}"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report whether a finished fleet deployment may advance its deployment ref"
    )
    parser.add_argument("--transcript", required=True, type=Path)
    parser.add_argument("--environment", required=True, choices=("develop", "prod"))
    parser.add_argument("--source-git-sha", required=True)
    baseline = parser.add_mutually_exclusive_group(required=True)
    baseline.add_argument("--expected-baseline-git-sha")
    baseline.add_argument("--initial", action="store_true")
    args = parser.parse_args(argv)

    try:
        transcript = args.transcript.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"cannot read the executor transcript: {exc}", file=sys.stderr)
        return 2
    try:
        record = extract_record(transcript)
        require_identity(
            record,
            environment=args.environment,
            source_git_sha=args.source_git_sha,
            expected_baseline=None if args.initial else args.expected_baseline_git_sha,
        )
    except RecordError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    refusal = promotion_refusal(record)
    if refusal is not None:
        print(f"deployment ref stays where it is: {refusal}", file=sys.stderr)
        print("promote=false")
        return 0
    print(
        f"deployment {record['deployment_id']} reached BACKEND_APPLIED; "
        f"the {args.environment} deployment ref may advance to {args.source_git_sha}",
        file=sys.stderr,
    )
    print("promote=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
