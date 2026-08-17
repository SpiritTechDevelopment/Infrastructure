"""Offline OpenSSL CA for develop/tests; no node private key is accepted."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

from .model import CertificateBundle, CertificateRequest, PkiError


IDENTITY_PATTERN = re.compile(
    r"^spiffe://spiritvpn/(?P<environment>develop|prod)/instance/[a-z0-9-]{1,63}$"
)


class LocalCertificateAuthorityAdapter:
    def __init__(self, state_directory: Path):
        self.state_directory = state_directory.resolve()

    def sign(self, request: CertificateRequest) -> CertificateBundle:
        match = IDENTITY_PATTERN.fullmatch(request.identity)
        if match is None or match.group("environment") != request.environment:
            raise PkiError("certificate identity does not belong to the requested environment")
        if request.environment not in {"develop", "prod"}:
            raise PkiError(f"unsupported PKI environment: {request.environment!r}")

        environment_directory = self.state_directory / request.environment
        environment_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(environment_directory, 0o700)
        ca_key = environment_directory / "ca.key"
        ca_certificate = environment_directory / "ca.crt"
        self._ensure_ca(request.environment, ca_key, ca_certificate)

        with tempfile.TemporaryDirectory(prefix="signing-", dir=environment_directory) as temporary:
            work = Path(temporary)
            csr_path = work / "request.csr"
            certificate_path = work / "certificate.pem"
            extensions_path = work / "extensions.cnf"
            csr_path.write_bytes(request.csr_pem)
            extensions_path.write_text(
                "\n".join(
                    (
                        "basicConstraints=critical,CA:FALSE",
                        "keyUsage=critical,digitalSignature,keyEncipherment",
                        "extendedKeyUsage=serverAuth,clientAuth",
                        f"subjectAltName=URI:{request.identity}",
                        "",
                    )
                ),
                encoding="utf-8",
            )
            self._run("req", "-in", str(csr_path), "-verify", "-noout")
            csr_text = self._run("req", "-in", str(csr_path), "-noout", "-text").stdout.decode(
                "utf-8", errors="replace"
            )
            if f"URI:{request.identity}" not in csr_text:
                raise PkiError("CSR does not contain the requested machine identity")
            self._run(
                "x509",
                "-req",
                "-in",
                str(csr_path),
                "-CA",
                str(ca_certificate),
                "-CAkey",
                str(ca_key),
                "-CAcreateserial",
                "-days",
                "30",
                "-sha256",
                "-extfile",
                str(extensions_path),
                "-out",
                str(certificate_path),
            )
            self._run(
                "verify",
                "-CAfile",
                str(ca_certificate),
                str(certificate_path),
            )
            certificate = certificate_path.read_bytes()
            ca_pem = ca_certificate.read_bytes()
            return CertificateBundle(
                environment=request.environment,
                identity=request.identity,
                certificate_chain_pem=certificate + ca_pem,
                ca_certificate_pem=ca_pem,
            )

    def _ensure_ca(self, environment: str, key: Path, certificate: Path) -> None:
        if key.exists() != certificate.exists():
            raise PkiError(f"local CA state is incomplete for environment {environment}")
        if key.exists():
            if key.is_symlink() or certificate.is_symlink():
                raise PkiError("refusing symlinked local CA material")
            os.chmod(key, 0o600)
            return
        self._run(
            "genpkey",
            "-algorithm",
            "EC",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-out",
            str(key),
        )
        os.chmod(key, 0o600)
        self._run(
            "req",
            "-x509",
            "-new",
            "-key",
            str(key),
            "-sha256",
            "-days",
            "3650",
            "-subj",
            f"/CN=SpiritVPN {environment} local CA",
            "-out",
            str(certificate),
        )
        os.chmod(certificate, 0o644)

    @staticmethod
    def _run(*arguments: str) -> subprocess.CompletedProcess[bytes]:
        try:
            result = subprocess.run(
                ["openssl", *arguments],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except OSError as exc:
            raise PkiError(f"cannot execute openssl: {exc}") from exc
        if result.returncode != 0:
            operation = arguments[0] if arguments else "command"
            raise PkiError(f"openssl {operation} failed")
        return result
