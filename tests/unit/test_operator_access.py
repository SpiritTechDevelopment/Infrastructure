"""Проверки выдачи и отзыва операторского доступа.

Здесь проверяется не «скрипт отработал», а два свойства, ради которых он и
написан: в объявления попадают только публичные части, и отзыв действительно
закрывает доступ. Второе особенно: забытая перешифровка не ломает ничего
видимого — отозванный оператор просто продолжает читать всё.

Криптография настоящая: тест создаёт age-ключи и вызывает sops. Подделка sops
заглушкой проверяла бы соглашение о вызовах, а не то, кто в итоге способен
расшифровать файл, — то есть ровно не то свойство.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
ACCESS = REPO_ROOT / "scripts" / "operator-access.py"
IDENTITY = REPO_ROOT / "scripts" / "operator-identity.py"

TOOLS_PRESENT = all(
    shutil.which(tool) for tool in ("sops", "age-keygen", "ssh-keygen", "wg")
)


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


IDENTITY_MODULE = load(IDENTITY, "operator_identity")


class EnrollmentFingerprintTests(unittest.TestCase):
    def test_fingerprint_covers_every_key(self) -> None:
        base = {
            "schema_version": 1,
            "operator": "roman",
            "age_recipient": "age1aaa",
            "ssh_public_key": "ssh-ed25519 AAAA",
            "wireguard_public_key": "d2lyZWd1YXJk",
        }
        original = IDENTITY_MODULE.enrollment_fingerprint(base)
        for field in ("age_recipient", "ssh_public_key", "wireguard_public_key"):
            altered = dict(base)
            altered[field] = altered[field] + "x"
            with self.subTest(field=field):
                self.assertNotEqual(
                    original,
                    IDENTITY_MODULE.enrollment_fingerprint(altered),
                    "подмена ключа обязана менять отпечаток, иначе сверка вслух бесполезна",
                )

    def test_fingerprint_is_stable(self) -> None:
        request = {"schema_version": 1, "operator": "a", "age_recipient": "age1a"}
        self.assertEqual(
            IDENTITY_MODULE.enrollment_fingerprint(request),
            IDENTITY_MODULE.enrollment_fingerprint(dict(reversed(list(request.items())))),
            "отпечаток не должен зависеть от порядка ключей",
        )


@unittest.skipUnless(TOOLS_PRESENT, "нужны sops, age-keygen, ssh-keygen и wg")
class OperatorAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="operator-access-")
        self.addCleanup(self._temporary.cleanup)
        self.base = Path(self._temporary.name)
        self.root = self.base / "repo"
        (self.root / "desired" / "common").mkdir(parents=True)
        (self.root / "inventories" / "bootstrap").mkdir(parents=True)

        self.operator_key = self.keygen("operator")
        self.runner_key = self.keygen("runner")
        self.write_sops_config([self.operator_key], [self.operator_key, self.runner_key])
        self.write_bundle()
        self.write_desired()

    def keygen(self, name: str) -> str:
        path = self.base / f"{name}.txt"
        subprocess.run(["age-keygen", "-o", str(path)], check=True, capture_output=True)
        setattr(self, f"{name}_path", path)
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# public key:"):
                return line.split(":", 1)[1].strip()
        raise AssertionError("age-keygen не сообщил публичный ключ")

    def write_sops_config(self, platform: list[str], desired: list[str]) -> None:
        def block(recipients: list[str]) -> str:
            body = ",\n      ".join(recipients)
            return f"    age: >-\n      {body}\n"

        (self.root / ".sops.yaml").write_text(
            "creation_rules:\n"
            "  # Желаемое состояние: операторы и раннер.\n"
            "  - path_regex: ^desired/.*[.]ya?ml$\n" + block(desired) +
            "  # Контракт платформы: раннер намеренно исключён.\n"
            "  - path_regex: ^inventories/bootstrap/platform[.]sops[.]ya?ml$\n" + block(platform),
            encoding="utf-8",
        )

    def sops_encrypt(self, relative: str, document: dict) -> None:
        plaintext = yaml.safe_dump(document, sort_keys=False)
        result = subprocess.run(
            [
                "sops", "--config", str(self.root / ".sops.yaml"),
                "--filename-override", relative,
                "--encrypt", "--input-type", "yaml", "--output-type", "yaml",
                "/dev/stdin",
            ],
            input=plaintext, text=True, capture_output=True, check=False,
            cwd=str(self.root),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.root / relative).write_text(result.stdout, encoding="utf-8")

    def write_bundle(self) -> None:
        self.sops_encrypt(
            "inventories/bootstrap/platform.sops.yml",
            {
                "vars": {
                    "platform_operator_ssh_public_keys": [
                        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExisting spiritvpn-operator-pavel"
                    ],
                    "platform_wireguard_hub_addresses": {
                        "develop": "10.80.0.1/16",
                        "prod": "10.90.0.1/16",
                    },
                    "platform_wireguard_operator_peers": [
                        {
                            "id": "pavel",
                            "public_key": "aGVsbG9oZWxsb2hlbGxvaGVsbG9oZWxsb2hlbGxvaGU=",
                            "allowed_ips": ["10.80.0.10/32"],
                        }
                    ],
                }
            },
        )

    def write_desired(self) -> None:
        self.sops_encrypt("desired/common/components.yml", {"value": "placeholder"})

    def make_request(self, operator: str) -> tuple[Path, dict]:
        target = self.base / f"{operator}.yml"
        subprocess.run(
            [
                "python3", str(IDENTITY), "create",
                "--operator", operator,
                "--home", str(self.base / operator),
                "--output", str(target),
            ],
            check=True, capture_output=True,
        )
        return target, yaml.safe_load(target.read_text(encoding="utf-8"))

    def access(self, *arguments: str) -> subprocess.CompletedProcess:
        environment = {**os.environ, "SOPS_AGE_KEY_FILE": str(self.operator_path)}
        return subprocess.run(
            ["python3", str(ACCESS), "--root", str(self.root), *arguments],
            capture_output=True, text=True, env=environment,
        )

    def can_decrypt(self, identity: Path, relative: str) -> bool:
        result = subprocess.run(
            ["sops", "--decrypt", relative],
            capture_output=True, text=True, cwd=str(self.root),
            env={**os.environ, "SOPS_AGE_KEY_FILE": str(identity)},
        )
        return result.returncode == 0

    def test_grant_puts_only_public_material_into_declarations(self) -> None:
        request_path, request = self.make_request("roman")
        result = self.access(
            "grant", "--request", str(request_path),
            "--address", "10.80.0.11", "--assume-verified",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        # Ни один приватный файл нового оператора не должен встретиться в дереве.
        private = (self.base / "roman" / "sops" / "age-identity.txt").read_text(encoding="utf-8")
        secret_line = next(
            line for line in private.splitlines() if line.startswith("AGE-SECRET-KEY")
        )
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(
                    secret_line,
                    path.read_text(encoding="utf-8", errors="ignore"),
                    f"приватный ключ просочился в {path}",
                )

        identity = self.base / "roman" / "sops" / "age-identity.txt"
        self.assertTrue(self.can_decrypt(identity, "inventories/bootstrap/platform.sops.yml"))
        self.assertTrue(self.can_decrypt(identity, "desired/common/components.yml"))

    def test_runner_stays_outside_the_platform_contract(self) -> None:
        request_path, _ = self.make_request("roman")
        self.access(
            "grant", "--request", str(request_path),
            "--address", "10.80.0.11", "--assume-verified",
        )
        self.assertFalse(
            self.can_decrypt(self.runner_path, "inventories/bootstrap/platform.sops.yml"),
            "выдача оператора не должна открывать контракт платформы раннеру",
        )

    def test_revoke_actually_removes_decryption(self) -> None:
        request_path, request = self.make_request("roman")
        self.access(
            "grant", "--request", str(request_path),
            "--address", "10.80.0.11", "--assume-verified",
        )
        identity = self.base / "roman" / "sops" / "age-identity.txt"
        self.assertTrue(self.can_decrypt(identity, "desired/common/components.yml"))

        result = self.access(
            "revoke", "--operator", "roman",
            "--age-recipient", request["age_recipient"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for relative in (
            "inventories/bootstrap/platform.sops.yml",
            "desired/common/components.yml",
        ):
            with self.subTest(path=relative):
                self.assertFalse(
                    self.can_decrypt(identity, relative),
                    "отозванный оператор всё ещё расшифровывает — забыта перешифровка",
                )
        self.assertTrue(self.can_decrypt(self.operator_path, "desired/common/components.yml"))

    def test_address_outside_the_management_networks_is_refused(self) -> None:
        request_path, _ = self.make_request("roman")
        result = self.access(
            "grant", "--request", str(request_path),
            "--address", "192.0.2.5", "--assume-verified",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("вне управляющих сетей", result.stderr)

    def test_address_already_taken_is_refused(self) -> None:
        request_path, _ = self.make_request("roman")
        result = self.access(
            "grant", "--request", str(request_path),
            "--address", "10.80.0.10", "--assume-verified",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("уже выдан", result.stderr)

    def test_granting_the_same_operator_twice_is_refused(self) -> None:
        request_path, _ = self.make_request("roman")
        self.access(
            "grant", "--request", str(request_path),
            "--address", "10.80.0.11", "--assume-verified",
        )
        result = self.access(
            "grant", "--request", str(request_path),
            "--address", "10.80.0.12", "--assume-verified",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("уже включён", result.stderr)

    def test_revoking_the_last_operator_is_refused(self) -> None:
        result = self.access("revoke", "--operator", "pavel", "--age-recipient", self.operator_key)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("последнего оператора", result.stderr)

    def test_sops_config_keeps_its_comments(self) -> None:
        request_path, _ = self.make_request("roman")
        self.access(
            "grant", "--request", str(request_path),
            "--address", "10.80.0.11", "--assume-verified",
        )
        text = (self.root / ".sops.yaml").read_text(encoding="utf-8")
        self.assertIn("раннер намеренно исключён", text)
        self.assertIn("Желаемое состояние", text)


if __name__ == "__main__":
    unittest.main()
