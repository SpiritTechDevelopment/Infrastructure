from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

import yaml

from fleetctl.pki import (
    CertificateRequest,
    LocalCertificateAuthorityAdapter,
    PkiError,
    agent_dns_name,
    generate_key_and_csr,
)
from fleetctl.pki.issuance import _vault_targets
from fleetctl.validation import validate_environment


REPO_ROOT = Path(__file__).resolve().parents[2]
LIVE_DESIRED_SKIP_REASON = "encrypted repository desired state requires a trusted SOPS identity"

# The floor both roles enforce: control_tls_minimum_validity_seconds and
# pki_agent_minimum_validity_seconds are each 604800.
MINIMUM_VALIDITY_SECONDS = 604800


def run_openssl(*arguments: str) -> str:
    result = subprocess.run(
        ["openssl", *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout.decode("utf-8", errors="replace")


def generate_csr(
    directory: Path,
    *,
    common_name: str,
    subject_alt_names: Sequence[str],
    stem: str = "peer",
) -> tuple[Path, bytes]:
    key = directory / f"{stem}.key"
    csr = directory / f"{stem}.csr"
    csr_pem = generate_key_and_csr(
        key,
        csr,
        common_name=common_name,
        subject_alt_names=subject_alt_names,
    )
    return key, csr_pem


def agent_identity(environment: str, instance_id: str) -> str:
    return f"spiffe://spiritvpn/{environment}/instance/{instance_id}"


def sign_agent_server(
    adapter: LocalCertificateAuthorityAdapter,
    directory: Path,
    environment: str,
    instance_id: str,
):
    identity = agent_identity(environment, instance_id)
    dns = agent_dns_name(environment, instance_id)
    key, csr = generate_csr(
        directory,
        common_name=instance_id,
        subject_alt_names=(f"DNS:{dns}", f"URI:{identity}"),
    )
    bundle = adapter.sign(
        CertificateRequest(
            environment=environment,
            profile="agent-server",
            csr_pem=csr,
            identity=identity,
            dns_names=(dns,),
            subject=instance_id,
        )
    )
    return key, bundle


class LocalPkiTests(unittest.TestCase):
    def test_local_ca_signs_csr_without_receiving_node_private_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            node = root / "node"
            node.mkdir()
            ca_state = root / "ca"
            node_key, bundle = sign_agent_server(
                LocalCertificateAuthorityAdapter(ca_state), node, "develop", "develop-entry-nl-01"
            )
            chain = root / "chain.pem"
            ca = root / "ca.pem"
            chain.write_bytes(bundle.certificate_chain_pem)
            ca.write_bytes(bundle.ca_certificate_pem)
            text = run_openssl("x509", "-in", str(chain), "-noout", "-text")
            verification = run_openssl("verify", "-CAfile", str(ca), str(chain))

            self.assertTrue(node_key.exists())
            self.assertNotIn(node_key.read_bytes(), bundle.certificate_chain_pem)
            self.assertFalse(any(path.name.endswith(".key") for path in (ca_state / "develop").rglob("peer*")))
            self.assertEqual(os.stat(ca_state / "develop" / "ca.key").st_mode & 0o777, 0o600)

        self.assertIn(f"URI:{agent_identity('develop', 'develop-entry-nl-01')}", text)
        self.assertIn("OK", verification)

    def test_agent_certificate_satisfies_the_host_name_check_of_the_backend(self) -> None:
        """SpiritVPN/internal/nodeagent/tls.go dials with ServerName and full verification.

        A URI SAN alone leaves the handshake failing on the name, which is what
        the previous identity-only extfile produced.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance_id = "develop-exit-de-01"
            _, bundle = sign_agent_server(
                LocalCertificateAuthorityAdapter(root / "ca"), root, "develop", instance_id
            )
            leaf = root / "agent.crt"
            leaf.write_bytes(bundle.certificate_pem)
            run_openssl(
                "x509",
                "-in",
                str(leaf),
                "-noout",
                "-checkhost",
                agent_dns_name("develop", instance_id),
            )
            run_openssl("x509", "-in", str(leaf), "-noout", "-checkend", str(MINIMUM_VALIDITY_SECONDS))
            text = run_openssl("x509", "-in", str(leaf), "-noout", "-text")
        # serverAuth only. The agent serves; it never presents a client
        # certificate anywhere — its one outbound gRPC goes to local Xray on the
        # loopback with insecure credentials. Withholding clientAuth means Go
        # rejects this leaf during chain verification if it is ever replayed as
        # a client, one step before the identity allow-list would.
        self.assertIn("TLS Web Server Authentication", text)
        self.assertNotIn("TLS Web Client Authentication", text)

    def test_backend_server_certificate_carries_a_host_name_and_no_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = "backend.develop.internal"
            _, csr = generate_csr(root, common_name="backend", subject_alt_names=(f"DNS:{host}",))
            bundle = LocalCertificateAuthorityAdapter(root / "ca").sign(
                CertificateRequest(
                    environment="develop",
                    profile="backend-server",
                    csr_pem=csr,
                    dns_names=(host,),
                )
            )
            leaf = root / "server.crt"
            leaf.write_bytes(bundle.certificate_pem)
            # Exactly the assertion control_runtime makes before starting the backend.
            run_openssl("x509", "-in", str(leaf), "-noout", "-checkhost", host)
            text = run_openssl("x509", "-in", str(leaf), "-noout", "-text")
        self.assertIsNone(bundle.identity)
        self.assertNotIn("URI:", text)
        self.assertIn("TLS Web Server Authentication", text)
        self.assertNotIn("TLS Web Client Authentication", text)

    @unittest.skipIf(os.environ.get("SPIRITVPN_SKIP_LIVE_DESIRED") == "1", LIVE_DESIRED_SKIP_REASON)
    def test_issuance_names_where_the_bot_certificate_belongs(self) -> None:
        """Оператор получает путь из соглашения, а не из головы.

        Имя поля становится именем файла, и те же имена перечислены в роли
        control_runtime как проводка. Пустой ответ здесь означал бы церемонию,
        после которой оператор сам догадывается, в какое поле Vault класть
        файл, — и путь разошёлся бы с тем, что потребует выкатка.
        """
        state = validate_environment(REPO_ROOT, "develop")
        targets = _vault_targets(state, "customer-service")
        path = "kv/develop/control/bot/files"
        self.assertEqual(targets["certificate"], f"{path}#client.crt")
        self.assertEqual(targets["private_key"], f"{path}#client.key")
        self.assertEqual(targets["ca_certificate"], [f"{path}#server-ca.crt"])
        # Поддерево бэкенда сюда попасть не должно: это разные личности.
        self.assertNotIn("control/backend/", str(targets))

    def test_client_profiles_carry_their_service_identity_only(self) -> None:
        expected = {
            "backend-client": "spiffe://spiritvpn/develop/service/backend",
            "manifest-writer": "spiffe://spiritvpn/develop/service/manifest-writer",
            "customer-service": "spiffe://spiritvpn/develop/service/customer-service",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = LocalCertificateAuthorityAdapter(root / "ca")
            for profile, identity in expected.items():
                _, csr = generate_csr(
                    root,
                    common_name=profile,
                    subject_alt_names=(f"URI:{identity}",),
                    stem=profile,
                )
                bundle = adapter.sign(
                    CertificateRequest(environment="develop", profile=profile, csr_pem=csr)
                )
                leaf = root / f"{profile}.crt"
                leaf.write_bytes(bundle.certificate_pem)
                text = run_openssl("x509", "-in", str(leaf), "-noout", "-text")
                self.assertEqual(bundle.identity, identity)
                self.assertIn(f"URI:{identity}", text)
                self.assertNotIn("DNS:", text)
                self.assertIn("TLS Web Client Authentication", text)
                self.assertNotIn("TLS Web Server Authentication", text)

    def test_environment_identity_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance_id = "develop-entry-nl-01"
            identity = agent_identity("develop", instance_id)
            dns = agent_dns_name("develop", instance_id)
            _, csr = generate_csr(
                root,
                common_name=instance_id,
                subject_alt_names=(f"DNS:{dns}", f"URI:{identity}"),
            )
            with self.assertRaises(PkiError):
                LocalCertificateAuthorityAdapter(root / "ca").sign(
                    CertificateRequest(
                        environment="prod",
                        profile="agent-server",
                        csr_pem=csr,
                        identity=identity,
                        dns_names=(dns,),
                        subject=instance_id,
                    )
                )

    def test_a_csr_declaring_a_neighbouring_identity_is_rejected(self) -> None:
        """The declared identity is a strict superstring of the issued one.

        A substring test would sign it: `.../develop-entry-nl-01` occurs inside
        `.../develop-entry-nl-011`.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            neighbour = "develop-entry-nl-011"
            issued = "develop-entry-nl-01"
            _, csr = generate_csr(
                root,
                common_name=neighbour,
                subject_alt_names=(
                    f"DNS:{agent_dns_name('develop', neighbour)}",
                    f"URI:{agent_identity('develop', neighbour)}",
                ),
            )
            with self.assertRaises(PkiError):
                LocalCertificateAuthorityAdapter(root / "ca").sign(
                    CertificateRequest(
                        environment="develop",
                        profile="agent-server",
                        csr_pem=csr,
                        identity=agent_identity("develop", issued),
                        dns_names=(agent_dns_name("develop", issued),),
                        subject=issued,
                    )
                )

    def test_agent_host_name_must_describe_the_same_machine_as_the_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance_id = "develop-entry-nl-01"
            wrong = agent_dns_name("develop", "develop-exit-de-01")
            _, csr = generate_csr(
                root,
                common_name=instance_id,
                subject_alt_names=(
                    f"DNS:{wrong}",
                    f"URI:{agent_identity('develop', instance_id)}",
                ),
            )
            with self.assertRaises(PkiError):
                LocalCertificateAuthorityAdapter(root / "ca").sign(
                    CertificateRequest(
                        environment="develop",
                        profile="agent-server",
                        csr_pem=csr,
                        identity=agent_identity("develop", instance_id),
                        dns_names=(wrong,),
                        subject=instance_id,
                    )
                )

    def test_unknown_profile_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, csr = generate_csr(root, common_name="x", subject_alt_names=("DNS:x.develop.internal",))
            with self.assertRaises(PkiError):
                LocalCertificateAuthorityAdapter(root / "ca").sign(
                    CertificateRequest(environment="develop", profile="wildcard", csr_pem=csr)
                )

    def test_client_profiles_refuse_host_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = "spiffe://spiritvpn/develop/service/backend"
            _, csr = generate_csr(
                root,
                common_name="backend",
                subject_alt_names=("DNS:backend.develop.internal", f"URI:{identity}"),
            )
            with self.assertRaises(PkiError):
                LocalCertificateAuthorityAdapter(root / "ca").sign(
                    CertificateRequest(
                        environment="develop",
                        profile="backend-client",
                        csr_pem=csr,
                        dns_names=("backend.develop.internal",),
                    )
                )

    def test_private_key_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key, _ = generate_csr(root, common_name="x", subject_alt_names=("DNS:x.develop.internal",))
            original = key.read_bytes()
            with self.assertRaises(PkiError):
                generate_csr(root, common_name="x", subject_alt_names=("DNS:x.develop.internal",))
            self.assertEqual(key.read_bytes(), original)

    def test_develop_and_prod_have_different_ca_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = LocalCertificateAuthorityAdapter(root / "ca")
            bundles = []
            for environment in ("develop", "prod"):
                node = root / environment
                node.mkdir()
                _, bundle = sign_agent_server(
                    adapter, node, environment, f"{environment}-entry-nl-01"
                )
                bundles.append(bundle)
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
