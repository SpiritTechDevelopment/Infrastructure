from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from fleetctl.pki import CertificateRequest, LocalCertificateAuthorityAdapter, PkiError


REPO_ROOT = Path(__file__).resolve().parents[2]


def run_openssl(*arguments: str) -> str:
    result = subprocess.run(
        ["openssl", *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout.decode("utf-8", errors="replace")


def generate_csr(directory: Path, identity: str, instance_id: str) -> tuple[Path, bytes]:
    key = directory / "agent.key"
    csr = directory / "agent.csr"
    run_openssl(
        "genpkey",
        "-algorithm",
        "EC",
        "-pkeyopt",
        "ec_paramgen_curve:P-256",
        "-out",
        str(key),
    )
    run_openssl(
        "req",
        "-new",
        "-key",
        str(key),
        "-out",
        str(csr),
        "-subj",
        f"/CN={instance_id}",
        "-addext",
        f"subjectAltName=URI:{identity}",
    )
    return key, csr.read_bytes()


class LocalPkiTests(unittest.TestCase):
    def test_local_ca_signs_csr_without_receiving_node_private_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            node = root / "node"
            node.mkdir()
            ca_state = root / "ca"
            identity = "spiffe://spiritvpn/develop/instance/develop-entry-nl-01"
            node_key, csr = generate_csr(node, identity, "develop-entry-nl-01")
            bundle = LocalCertificateAuthorityAdapter(ca_state).sign(
                CertificateRequest(environment="develop", identity=identity, csr_pem=csr)
            )
            chain = root / "chain.pem"
            ca = root / "ca.pem"
            chain.write_bytes(bundle.certificate_chain_pem)
            ca.write_bytes(bundle.ca_certificate_pem)
            text = run_openssl("x509", "-in", str(chain), "-noout", "-text")
            verification = run_openssl("verify", "-CAfile", str(ca), str(chain))

            self.assertTrue(node_key.exists())
            self.assertNotIn(node_key.read_bytes(), bundle.certificate_chain_pem)
            self.assertFalse(any(path.name == "agent.key" for path in ca_state.rglob("*")))
            self.assertEqual(os.stat(ca_state / "develop" / "ca.key").st_mode & 0o777, 0o600)

        self.assertIn(f"URI:{identity}", text)
        self.assertIn("OK", verification)

    def test_environment_identity_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = "spiffe://spiritvpn/develop/instance/develop-entry-nl-01"
            _, csr = generate_csr(root, identity, "develop-entry-nl-01")
            with self.assertRaises(PkiError):
                LocalCertificateAuthorityAdapter(root / "ca").sign(
                    CertificateRequest(environment="prod", identity=identity, csr_pem=csr)
                )

    def test_develop_and_prod_have_different_ca_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = LocalCertificateAuthorityAdapter(root / "ca")
            bundles = []
            for environment in ("develop", "prod"):
                node = root / environment
                node.mkdir()
                instance_id = f"{environment}-entry-nl-01"
                identity = f"spiffe://spiritvpn/{environment}/instance/{instance_id}"
                _, csr = generate_csr(node, identity, instance_id)
                bundles.append(
                    adapter.sign(CertificateRequest(environment=environment, identity=identity, csr_pem=csr))
                )
            self.assertNotEqual(bundles[0].ca_certificate_pem, bundles[1].ca_certificate_pem)

    def test_ansible_roles_never_slurp_machine_private_keys(self) -> None:
        for relative in (
            "roles/pki_agent/tasks/main.yml",
            "roles/bootstrap_wireguard/tasks/main.yml",
        ):
            tasks = yaml.safe_load((REPO_ROOT / relative).read_text(encoding="utf-8"))
            slurped = [task["ansible.builtin.slurp"]["src"] for task in tasks if "ansible.builtin.slurp" in task]
            self.assertTrue(slurped)
            self.assertTrue(all("public_key" in source or "csr" in source for source in slurped))
            self.assertTrue(all("private_key" not in source for source in slurped))


if __name__ == "__main__":
    unittest.main()
