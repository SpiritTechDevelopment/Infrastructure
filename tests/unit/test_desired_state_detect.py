"""Behavioural tests for the detect step of desired-state-deploy.

The step decides, from one push, which of the three contours have to be
reconciled. Getting it wrong is silent in the worst direction: a change that
deploys nothing looks exactly like a change that needed nothing. So the step is
exercised against real commits in a throwaway repository rather than asserted
against its own source text.

`yq` is not installed on every workstation, so the two expressions the step uses
on `environment.yml` — `.spec.control` and `del(.spec.control)` — are served by a
small stand-in on PATH. The step only ever compares their output for equality,
which is what the stand-in reproduces; everything else under test is the
classification of paths, which is the part that changed.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]

YQ_STAND_IN = """#!/usr/bin/env python3
import sys
import yaml

expression = sys.argv[1]
document = yaml.safe_load(sys.stdin.read()) or {}
if expression == ".spec.control":
    selected = (document.get("spec") or {}).get("control")
elif expression == "del(.spec.control)":
    selected = document
    if isinstance(selected, dict) and isinstance(selected.get("spec"), dict):
        selected = dict(selected)
        selected["spec"] = {k: v for k, v in selected["spec"].items() if k != "control"}
else:
    raise SystemExit(f"stand-in does not implement: {expression}")
print(yaml.safe_dump(selected, sort_keys=True, allow_unicode=True))
"""

ENVIRONMENT_DOCUMENT = """apiVersion: spiritvpn.io/v1alpha1
kind: Environment
metadata:
  id: {environment}
spec:
  control:
    backend_release:
      digest: {digest}
  common_overrides:
    components:
      node_agent:
        digest: {agent_digest}
"""


def detect_script() -> str:
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "desired-state-deploy.yml").read_text(
            encoding="utf-8"
        )
    )
    for step in workflow["jobs"]["detect"]["steps"]:
        if step.get("id") == "split":
            return step["run"]
    raise AssertionError("desired-state-deploy.yml has no detect step with id 'split'")


class DesiredStateDetectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = detect_script()

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="detect-")
        self.addCleanup(self._temporary.cleanup)
        root = Path(self._temporary.name)
        self.repository = root / "repository"
        self.repository.mkdir()
        self.runner_temp = root / "runner-temp"
        self.runner_temp.mkdir()

        binaries = root / "bin"
        binaries.mkdir()
        stand_in = binaries / "yq"
        stand_in.write_text(YQ_STAND_IN, encoding="utf-8")
        stand_in.chmod(0o755)
        self.path = f"{binaries}{os.pathsep}{os.environ['PATH']}"

        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "detect-test")
        self.git("config", "user.email", "detect@spiritvpn.invalid")
        for environment in ("develop", "prod"):
            for area in ("nodes", "fleets", "instances", "platform"):
                (self.repository / "desired" / "environments" / environment / area).mkdir(
                    parents=True
                )
            self.write(
                f"desired/environments/{environment}/environment.yml",
                ENVIRONMENT_DOCUMENT.format(
                    environment=environment, digest="sha256:aaa", agent_digest="sha256:bbb"
                ),
            )
        self.write("roles/xray/tasks/main.yml", "- name: noop\n")
        self.write("docs/guide.md", "# guide\n")
        self.base = self.commit("base")

    def git(self, *arguments: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(self.repository), *arguments], text=True
        ).strip()

    def write(self, relative: str, content: str) -> None:
        path = self.repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def detect(self, before: str, after: str) -> dict[str, list[str]]:
        output = self.runner_temp / "github-output"
        output.write_text("", encoding="utf-8")
        result = subprocess.run(
            ["bash", "-c", self.script],
            cwd=self.repository,
            env={
                **os.environ,
                "PATH": self.path,
                "BEFORE": before,
                "AFTER": after,
                "RUNNER_TEMP": str(self.runner_temp),
                "GITHUB_OUTPUT": str(output),
            },
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed: dict[str, list[str]] = {}
        for line in output.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            parsed[key] = yaml.safe_load(value)
        return parsed

    def assert_areas(
        self, areas: dict[str, list[str]], *, platform: list, control: list, fleet: list
    ) -> None:
        self.assertEqual(areas, {"platform": platform, "control": control, "fleet": fleet})

    # Ровно тот сценарий, ради которого триггер и расширялся.
    def test_a_new_node_declaration_reaches_the_fleet(self) -> None:
        self.write("desired/environments/develop/nodes/develop-entry-de.yml", "spec: {}\n")
        head = self.commit("add a node")
        self.assert_areas(
            self.detect(self.base, head), platform=[], control=[], fleet=["develop"]
        )

    def test_removing_a_node_declaration_also_reaches_the_fleet(self) -> None:
        self.write("desired/environments/develop/nodes/develop-entry-de.yml", "spec: {}\n")
        added = self.commit("add a node")
        (self.repository / "desired/environments/develop/nodes/develop-entry-de.yml").unlink()
        head = self.commit("remove the node")
        self.assert_areas(
            self.detect(added, head), platform=[], control=[], fleet=["develop"]
        )

    def test_fleet_and_instance_declarations_reach_the_fleet(self) -> None:
        self.write("desired/environments/develop/fleets/develop-fleet-eu.yml", "spec: {}\n")
        self.write("desired/environments/develop/instances/develop-entry-de-01.yml", "spec: {}\n")
        head = self.commit("declare a fleet and an instance")
        self.assert_areas(
            self.detect(self.base, head), platform=[], control=[], fleet=["develop"]
        )

    def test_environment_platform_declaration_reaches_the_platform(self) -> None:
        self.write("desired/environments/develop/platform/observability.yml", "spec: {}\n")
        head = self.commit("declare platform state")
        self.assert_areas(
            self.detect(self.base, head), platform=["develop"], control=[], fleet=[]
        )

    def test_single_topology_document_reaches_every_environment_contour(self) -> None:
        self.write(
            "desired/environments/develop/topology.sops.yml",
            "apiVersion: spiritvpn.io/v1alpha1\nkind: EnvironmentTopology\n",
        )
        head = self.commit("change the complete topology")
        self.assert_areas(
            self.detect(self.base, head),
            platform=["develop"],
            control=["develop"],
            fleet=["develop"],
        )

    # Разделение по поддеревьям сохраняется: релиз бэкенда не передеплоивает ноды.
    def test_a_backend_release_reaches_control_only(self) -> None:
        self.write(
            "desired/environments/develop/environment.yml",
            ENVIRONMENT_DOCUMENT.format(
                environment="develop", digest="sha256:ccc", agent_digest="sha256:bbb"
            ),
        )
        head = self.commit("backend release")
        self.assert_areas(
            self.detect(self.base, head), platform=[], control=["develop"], fleet=[]
        )

    def test_an_agent_release_reaches_the_fleet_only(self) -> None:
        self.write(
            "desired/environments/develop/environment.yml",
            ENVIRONMENT_DOCUMENT.format(
                environment="develop", digest="sha256:aaa", agent_digest="sha256:ddd"
            ),
        )
        head = self.commit("agent release")
        self.assert_areas(
            self.detect(self.base, head), platform=[], control=[], fleet=["develop"]
        )

    # Неопознанный путь означает «может задеть что угодно», а не «ничего».
    def test_a_role_change_reaches_every_contour(self) -> None:
        self.write("roles/xray/tasks/main.yml", "- name: changed\n")
        head = self.commit("change a role")
        self.assert_areas(
            self.detect(self.base, head),
            platform=["develop"],
            control=["develop"],
            fleet=["develop"],
        )

    def test_an_unknown_new_directory_reaches_every_contour(self) -> None:
        self.write("roles/brand-new-role/tasks/main.yml", "- name: new\n")
        head = self.commit("add a role that no list knows about")
        self.assert_areas(
            self.detect(self.base, head),
            platform=["develop"],
            control=["develop"],
            fleet=["develop"],
        )

    def test_prod_never_reaches_the_automatic_path(self) -> None:
        self.write("desired/environments/prod/nodes/prod-entry-de.yml", "spec: {}\n")
        self.write("roles/xray/tasks/main.yml", "- name: changed\n")
        head = self.commit("touch prod and a role")
        areas = self.detect(self.base, head)
        for area, environments in areas.items():
            with self.subTest(area=area):
                self.assertNotIn("prod", environments)

    def test_the_first_push_to_a_branch_deploys_nothing(self) -> None:
        self.write("desired/environments/develop/nodes/develop-entry-de.yml", "spec: {}\n")
        head = self.commit("add a node")
        self.assert_areas(self.detect("0" * 40, head), platform=[], control=[], fleet=[])


if __name__ == "__main__":
    unittest.main()
