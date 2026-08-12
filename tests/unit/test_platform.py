from __future__ import annotations

import base64
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from fleetctl.adapters import (
    PlatformArtifactsError,
    validate_platform_artifacts,
    validate_platform_known_hosts,
    write_rendered_files,
)
from fleetctl.compiler import PlatformNotDeclared, render_platform_files
from fleetctl.validation import DesiredStateInvalid, validate_environment


REPO_ROOT = Path(__file__).resolve().parents[2]
VALID_DESIRED = REPO_ROOT / "tests" / "fixtures" / "valid" / "desired"


class PlatformFoundationTests(unittest.TestCase):
    def test_platform_descriptor_compiles_deterministically(self) -> None:
        state = validate_environment(REPO_ROOT, "develop", desired_root=VALID_DESIRED)
        first = render_platform_files(state)
        second = render_platform_files(state)
        self.assertEqual(first, second)
        plan = json.loads(first["platform-plan.json"])
        self.assertEqual(plan["platform"]["id"], "develop-platform")
        self.assertEqual(plan["vault"]["api"]["bind_address"], "127.0.0.1")
        self.assertEqual(plan["vault"]["mounts"], {"kv": "kv/develop", "pki": "pki/develop"})
        self.assertEqual(
            plan["github_actions"]["oidc"]["bound_subject"],
            "repo:example/spiritvpn:environment:develop",
        )
        self.assertFalse(plan["automation_boundary"]["vault_init_is_automatic"])
        self.assertFalse(plan["automation_boundary"]["vault_unseal_is_automatic"])

    def test_platform_artifact_boundary_accepts_only_matching_generated_inputs(self) -> None:
        state = validate_environment(REPO_ROOT, "develop", desired_root=VALID_DESIRED)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "platform"
            write_rendered_files(output, render_platform_files(state))
            self.assertEqual(validate_platform_artifacts(output, "develop"), "develop-platform")
            plan_path = output / "platform-plan.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["vault"]["api"]["bind_address"] = "0.0.0.0"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            with self.assertRaises(PlatformArtifactsError):
                validate_platform_artifacts(output, "develop")

    def test_platform_render_fails_when_descriptor_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            desired = Path(temporary) / "desired"
            shutil.copytree(VALID_DESIRED, desired)
            shutil.rmtree(desired / "environments" / "develop" / "platform")
            state = validate_environment(REPO_ROOT, "develop", desired_root=desired)
            self.assertIsNone(state.platform)
            with self.assertRaises(PlatformNotDeclared):
                render_platform_files(state)

    def test_platform_requires_a_pinned_vault_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            desired = Path(temporary) / "desired"
            shutil.copytree(VALID_DESIRED, desired)
            components_path = desired / "common" / "components.yml"
            components = yaml.safe_load(components_path.read_text(encoding="utf-8"))
            components["components"]["vault"]["digest"] = None
            components_path.write_text(yaml.safe_dump(components, sort_keys=False), encoding="utf-8")
            with self.assertRaises(DesiredStateInvalid) as raised:
                validate_environment(REPO_ROOT, "develop", desired_root=desired)
        self.assertIn("PLATFORM_VAULT_DIGEST", {issue.code for issue in raised.exception.issues})

    def test_platform_rejects_non_global_management_address(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            desired = Path(temporary) / "desired"
            shutil.copytree(VALID_DESIRED, desired)
            platform_path = desired / "environments" / "develop" / "platform" / "develop-platform.yml"
            platform = yaml.safe_load(platform_path.read_text(encoding="utf-8"))
            platform["spec"]["public_address"] = "192.0.2.10"
            platform_path.write_text(yaml.safe_dump(platform, sort_keys=False), encoding="utf-8")
            with self.assertRaises(DesiredStateInvalid) as raised:
                validate_environment(REPO_ROOT, "develop", desired_root=desired)
        self.assertIn("PLATFORM_PUBLIC_ADDRESS", {issue.code for issue in raised.exception.issues})

    def test_known_hosts_must_match_a_reviewed_fingerprint(self) -> None:
        state = validate_environment(REPO_ROOT, "develop", desired_root=VALID_DESIRED)
        raw_key = b"fixture-public-host-key"
        fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(raw_key).digest()).decode().rstrip("=")
        object.__setattr__(state.platform, "host_key_fingerprints", (fingerprint,))
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "platform"
            write_rendered_files(output, render_platform_files(state))
            known_hosts = Path(temporary) / "known_hosts"
            known_hosts.write_text(
                "1.1.1.2 ssh-ed25519 " + base64.b64encode(raw_key).decode() + "\n",
                encoding="utf-8",
            )
            self.assertEqual(validate_platform_known_hosts(output, known_hosts), fingerprint)
            known_hosts.write_text("1.1.1.2 ssh-ed25519 YW5vdGhlci1rZXk=\n", encoding="utf-8")
            with self.assertRaises(PlatformArtifactsError):
                validate_platform_known_hosts(output, known_hosts)

    def test_platform_bootstrap_is_explicit_and_does_not_automate_vault_init(self) -> None:
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn("refusing platform SSH/mutation: set APPLY=1 explicitly", makefile)
        self.assertIn("PLATFORM_BOOTSTRAP_VARS is required", makefile)
        bootstrap = (REPO_ROOT / "playbooks" / "platform" / "bootstrap.yml").read_text(encoding="utf-8")
        self.assertIn("platform_vault", bootstrap)
        role_files = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((REPO_ROOT / "roles" / "platform_vault").rglob("*"))
            if path.is_file()
        )
        self.assertNotIn("operator init", role_files)
        self.assertNotIn("operator unseal", role_files)
        workflow = (REPO_ROOT / ".github" / "workflows" / "platform-readiness.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("platform-known-hosts-check", workflow)
        self.assertNotIn("ssh-keyscan", workflow)
        self.assertNotIn("id-token: write", workflow)
        self.assertNotIn("fleet-platform-bootstrap", workflow)
        self.assertNotIn("fleet-deploy", workflow)


if __name__ == "__main__":
    unittest.main()
