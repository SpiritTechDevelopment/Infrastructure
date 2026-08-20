from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "sops-envelope-check.py"
SPEC = importlib.util.spec_from_file_location("sops_envelope_check", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SopsEnvelopeCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="sops-envelope-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.environment_root = self.root / "desired" / "environments" / "develop"
        self.environment_root.mkdir(parents=True)
        common_root = self.root / "desired" / "common"
        common_root.mkdir(parents=True)
        for filename in MODULE.COMMON_FILES:
            self.write_scalar_ciphertext(common_root / filename)
        self.write_scalar_ciphertext(self.root / "desired" / "fleet-ids.yml")

    def sops_metadata(self) -> dict:
        encrypted = "ENC[AES256_GCM,data:x,iv:y,tag:z,type:str]"
        return {
            "age": [
                {
                    "recipient": f"age1example{suffix}",
                    "enc": "-----BEGIN AGE ENCRYPTED FILE-----\nx",
                }
                for suffix in ("one", "two", "three")
            ],
            "mac": encrypted,
        }

    def write_scalar_ciphertext(self, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            yaml.safe_dump(
                {
                    "value": "ENC[AES256_GCM,data:x,iv:y,tag:z,type:str]",
                    "sops": self.sops_metadata(),
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    def topology(self) -> dict:
        encrypted = "ENC[AES256_GCM,data:x,iv:y,tag:z,type:str]"
        return {
            "apiVersion": "spiritvpn.io/v1alpha1",
            "kind": "EnvironmentTopology",
            "metadata": {"id": "develop"},
            "spec": {"objects": [encrypted]},
            "sops": {**self.sops_metadata(), "encrypted_regex": "^(spec)$"},
        }

    def write_topology(self, document: dict | None = None) -> Path:
        target = self.environment_root / "topology.sops.yml"
        target.write_text(
            yaml.safe_dump(document or self.topology(), sort_keys=False),
            encoding="utf-8",
        )
        return target

    def test_valid_ciphertext_envelope_needs_no_private_identity(self) -> None:
        self.write_topology()
        self.assertEqual(MODULE.check(self.root), [])

    def test_plaintext_scalar_inside_spec_is_rejected(self) -> None:
        document = self.topology()
        document["spec"]["objects"].append("plaintext")
        self.write_topology(document)
        self.assertTrue(
            any("plaintext scalar" in issue for issue in MODULE.check(self.root))
        )

    def test_standalone_environment_yaml_is_rejected(self) -> None:
        self.write_topology()
        (self.environment_root / "environment.yml").write_text(
            "kind: Environment\n",
            encoding="utf-8",
        )
        self.assertTrue(
            any(
                "mixed environment YAML" in issue
                for issue in MODULE.check(self.root)
            )
        )

    def test_unexpected_common_yaml_is_rejected(self) -> None:
        self.write_topology()
        (self.root / "desired" / "common" / "leak.yml").write_text(
            "value: plaintext\n",
            encoding="utf-8",
        )
        self.assertTrue(
            any("unexpected or plaintext" in issue for issue in MODULE.check(self.root))
        )


if __name__ == "__main__":
    unittest.main()
