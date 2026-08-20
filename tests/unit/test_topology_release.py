from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "topology-release.py"


class TopologyReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="topology-release-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.environment_root = self.root / "desired" / "environments" / "develop"
        self.environment_root.mkdir(parents=True)
        (self.root / ".sops.yaml").write_text("creation_rules: []\n", encoding="utf-8")

        binary_dir = self.root / "bin"
        binary_dir.mkdir()
        fake_sops = binary_dir / "sops"
        fake_sops.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
for argument in "$@"; do
  if [[ "$argument" == --decrypt ]]; then
    cat "${!#}"
    exit 0
  fi
done
cat
""",
            encoding="utf-8",
        )
        fake_sops.chmod(0o755)
        self.environment = {**os.environ, "PATH": f"{binary_dir}:{os.environ['PATH']}"}

    def run_script(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(SCRIPT), *arguments],
            text=True,
            capture_output=True,
            check=False,
            env=self.environment,
        )

    def environment_object(self) -> dict:
        return {
            "apiVersion": "spiritvpn.io/v1alpha1",
            "kind": "Environment",
            "metadata": {"id": "develop"},
            "spec": {
                "dns_zone": "example.invalid",
                "management_network": "10.80.0.0/16",
                "backend_endpoint": "backend.internal:9443",
                "secret_store": {"kv": "kv/develop", "pki": "pki/develop"},
                "control": {
                    "backend_release": {
                        "source_git_sha": "1" * 40,
                        "backend_image": {
                            "repository": "ghcr.io/example/backend",
                            "digest": f"sha256:{'1' * 64}",
                        },
                        "migration_image": {
                            "repository": "ghcr.io/example/migrate",
                            "digest": f"sha256:{'2' * 64}",
                        },
                    },
                    "bot": {
                        "release": {
                            "source_git_sha": "2" * 40,
                            "image": {
                                "repository": "ghcr.io/example/bot",
                                "digest": f"sha256:{'3' * 64}",
                            },
                        }
                    },
                },
            },
        }

    def test_pack_wraps_existing_objects_without_plaintext_files(self) -> None:
        source = self.environment_root / "environment.yml"
        source.write_text(yaml.safe_dump(self.environment_object()), encoding="utf-8")
        result = self.run_script(
            "pack", "--root", str(self.root), "--environment", "develop"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        topology = yaml.safe_load(result.stdout)
        self.assertEqual(topology["kind"], "EnvironmentTopology")
        self.assertEqual(topology["metadata"]["id"], "develop")
        self.assertEqual(topology["spec"]["objects"], [self.environment_object()])
        self.assertFalse((self.environment_root / "topology.sops.yml").exists())

    def test_release_bump_changes_only_the_selected_release(self) -> None:
        topology = {
            "apiVersion": "spiritvpn.io/v1alpha1",
            "kind": "EnvironmentTopology",
            "metadata": {"id": "develop"},
            "spec": {"objects": [self.environment_object()]},
        }
        target = self.environment_root / "topology.sops.yml"
        target.write_text(yaml.safe_dump(topology), encoding="utf-8")
        result = self.run_script(
            "bump",
            "--root",
            str(self.root),
            "--environment",
            "develop",
            "--component",
            "bot-release",
            "--release-source-git-sha",
            "a" * 40,
            "--repository",
            "ghcr.io/example/new-bot",
            "--digest",
            f"sha256:{'b' * 64}",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        updated = yaml.safe_load(result.stdout)
        environment = updated["spec"]["objects"][0]
        release = environment["spec"]["control"]["bot"]["release"]
        self.assertEqual(release["source_git_sha"], "a" * 40)
        self.assertEqual(release["image"]["repository"], "ghcr.io/example/new-bot")
        self.assertEqual(release["image"]["digest"], f"sha256:{'b' * 64}")
        self.assertEqual(
            environment["spec"]["control"]["backend_release"],
            self.environment_object()["spec"]["control"]["backend_release"],
        )

    def test_control_release_requires_a_second_pinned_image(self) -> None:
        target = self.environment_root / "topology.sops.yml"
        target.write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "spiritvpn.io/v1alpha1",
                    "kind": "EnvironmentTopology",
                    "metadata": {"id": "develop"},
                    "spec": {"objects": [self.environment_object()]},
                }
            ),
            encoding="utf-8",
        )
        result = self.run_script(
            "bump",
            "--root",
            str(self.root),
            "--environment",
            "develop",
            "--component",
            "control-release",
            "--release-source-git-sha",
            "a" * 40,
            "--repository",
            "ghcr.io/example/backend",
            "--digest",
            f"sha256:{'b' * 64}",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid migration repository", result.stderr)


if __name__ == "__main__":
    unittest.main()
