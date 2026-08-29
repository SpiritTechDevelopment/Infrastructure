from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

SOURCE_SHA = "a" * 40
BASELINE_SHA = "b" * 40

# Ansible writes to the same stdout the coordinator later prints its record on,
# so the parser has to survive real play output — including a task that dumps
# JSON of its own.
ANSIBLE_NOISE = """
PLAY [Configure the fleet] *****************************************************

TASK [node_runtime : Render compose] *******************************************
ok: [develop-entry-ru-01] => {
    "changed": false,
    "path": "/etc/spiritvpn/compose.yml"
}

PLAY RECAP *********************************************************************
develop-entry-ru-01        : ok=42   changed=0    unreachable=0    failed=0
"""


def load_module():
    path = REPO_ROOT / "scripts" / "deployment-record.py"
    spec = importlib.util.spec_from_file_location("spiritvpn_deployment_record", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_record(**overrides):
    record = {
        "schema_version": 1,
        "deployment_id": f"develop-{SOURCE_SHA[:12]}",
        "environment": "develop",
        "source_git_sha": SOURCE_SHA,
        "baseline_git_sha": BASELINE_SHA,
        "dry_run": False,
        "status": "RECONCILED",
        "deployment_ref_updated": False,
        "diagnostic": "Infrastructure, backend manifest and DNS reached the reviewed source.",
        "dns_apply": {"status": "APPLIED", "change_count": 1, "record_count": 2},
        "steps": [
            {"name": "readiness_gates", "status": "COMPLETED"},
            {"name": "apply_dns", "status": "COMPLETED"},
        ],
    }
    record.update(overrides)
    return record


class DeploymentRecordTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()

    def run_script(self, transcript: str, *arguments: str):
        with tempfile.TemporaryDirectory(prefix="deployment-record-") as directory:
            path = Path(directory) / "transcript.txt"
            path.write_text(transcript, encoding="utf-8")
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = self.module.main(["--transcript", str(path), *arguments])
        return code, stdout.getvalue(), stderr.getvalue()

    def run_for_record(self, record, *arguments: str):
        transcript = ANSIBLE_NOISE + json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        return self.run_script(
            transcript,
            "--environment",
            "develop",
            "--source-git-sha",
            SOURCE_SHA,
            *(arguments or ("--expected-baseline-git-sha", BASELINE_SHA)),
        )

    def test_applied_deployment_may_advance_the_ref(self) -> None:
        code, stdout, _ = self.run_for_record(build_record())
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "promote=true\n")

    def test_initial_deployment_expects_no_baseline(self) -> None:
        code, stdout, _ = self.run_for_record(
            build_record(baseline_git_sha=None), "--initial"
        )
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "promote=true\n")

    def test_initial_flag_refuses_a_record_that_had_a_baseline(self) -> None:
        code, stdout, stderr = self.run_for_record(build_record(), "--initial")
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("baseline_git_sha", stderr)

    def test_dry_run_does_not_advance_the_ref(self) -> None:
        # A dry run reaches WAITING_FOR_BACKEND and exits zero. The exit code
        # alone would promote it.
        code, stdout, stderr = self.run_for_record(
            build_record(dry_run=True, status="WAITING_FOR_BACKEND")
        )
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "promote=false\n")
        self.assertIn("dry run", stderr)

    def test_apply_that_never_reached_the_backend_does_not_advance_the_ref(self) -> None:
        code, stdout, stderr = self.run_for_record(
            build_record(status="WAITING_FOR_BACKEND", diagnostic="no RPC was sent")
        )
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "promote=false\n")
        self.assertIn("no RPC was sent", stderr)

    def test_backend_without_dns_does_not_advance_the_ref(self) -> None:
        for status in ("BACKEND_APPLIED", "WAITING_FOR_DNS"):
            with self.subTest(status=status):
                code, stdout, stderr = self.run_for_record(
                    build_record(status=status, diagnostic="DNS was not applied")
                )
                self.assertEqual(code, 0)
                self.assertEqual(stdout, "promote=false\n")
                self.assertIn("DNS was not applied", stderr)

    def test_record_for_another_deployment_is_refused(self) -> None:
        for overrides in (
            {"environment": "prod", "deployment_id": f"prod-{SOURCE_SHA[:12]}"},
            {"source_git_sha": "c" * 40},
            {"deployment_id": "develop-000000000000"},
            {"baseline_git_sha": "d" * 40},
            {"schema_version": 2},
        ):
            with self.subTest(overrides=overrides):
                code, stdout, _ = self.run_for_record(build_record(**overrides))
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")

    def test_record_claiming_the_ref_already_moved_is_refused(self) -> None:
        code, stdout, stderr = self.run_for_record(build_record(deployment_ref_updated=True))
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("already claims", stderr)

    def test_transcript_without_a_record_is_refused(self) -> None:
        code, stdout, stderr = self.run_script(
            ANSIBLE_NOISE,
            "--environment",
            "develop",
            "--source-git-sha",
            SOURCE_SHA,
            "--expected-baseline-git-sha",
            BASELINE_SHA,
        )
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("no deployment record", stderr)

    def test_truncated_record_is_refused_rather_than_half_read(self) -> None:
        rendered = json.dumps(build_record(), ensure_ascii=False, indent=2, sort_keys=True)
        code, stdout, _ = self.run_script(
            ANSIBLE_NOISE + rendered[: len(rendered) // 2],
            "--environment",
            "develop",
            "--source-git-sha",
            SOURCE_SHA,
            "--expected-baseline-git-sha",
            BASELINE_SHA,
        )
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")

    def test_the_last_record_wins_over_an_earlier_one(self) -> None:
        # A resumed run prints one record per attempt into the same log; the one
        # that describes this deployment is the last.
        stale = json.dumps(
            build_record(status="FAILED", diagnostic="earlier attempt"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        current = json.dumps(build_record(), ensure_ascii=False, indent=2, sort_keys=True)
        code, stdout, _ = self.run_script(
            f"{stale}\n{ANSIBLE_NOISE}\n{current}\n",
            "--environment",
            "develop",
            "--source-git-sha",
            SOURCE_SHA,
            "--expected-baseline-git-sha",
            BASELINE_SHA,
        )
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "promote=true\n")


if __name__ == "__main__":
    unittest.main()
