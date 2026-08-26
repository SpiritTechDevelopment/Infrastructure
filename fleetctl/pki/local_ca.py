"""Локальный OpenSSL CA с отдельным корнем для каждой среды."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from .model import (
    ENVIRONMENTS,
    HOST_NAME,
    INSTANCE_IDENTITY,
    KEY_USAGE,
    SERVICE_IDENTITY,
    CertificateBundle,
    CertificateRequest,
    PkiError,
    agent_dns_name,
)


# Автоматическое обновление пока не работает, поэтому leaf действует год.
DEFAULT_VALIDITY_DAYS = 365
CA_VALIDITY_DAYS = 3650

_SAN_HEADER = "X509v3 Subject Alternative Name:"


class LocalCertificateAuthorityAdapter:
    def __init__(self, state_directory: Path, validity_days: int = DEFAULT_VALIDITY_DAYS):
        self.state_directory = state_directory.resolve()
        self.validity_days = validity_days

    def sign(self, request: CertificateRequest) -> CertificateBundle:
        profile = request.resolved_profile()
        if request.environment not in ENVIRONMENTS:
            raise PkiError(f"unsupported PKI environment: {request.environment!r}")

        identity = self._resolve_identity(request)
        dns_names = self._resolve_dns_names(request)
        expected_sans = request.subject_alt_names()
        if not expected_sans:
            raise PkiError(f"profile {profile.name} would produce a certificate without any SAN")

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
                        f"keyUsage=critical,{','.join(KEY_USAGE)}",
                        f"extendedKeyUsage={','.join(profile.extended_key_usage)}",
                        f"subjectAltName={','.join(expected_sans)}",
                        "",
                    )
                ),
                encoding="utf-8",
            )
            self._run("req", "-in", str(csr_path), "-verify", "-noout")
            csr_text = self._run("req", "-in", str(csr_path), "-noout", "-text").stdout.decode(
                "utf-8", errors="replace"
            )
            self._require_declared_sans(csr_text, expected_sans)
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
                str(self.validity_days),
                "-sha256",
                "-extfile",
                str(extensions_path),
                "-out",
                str(certificate_path),
            )
            self._run("verify", "-CAfile", str(ca_certificate), str(certificate_path))
            certificate = certificate_path.read_bytes()
            ca_pem = ca_certificate.read_bytes()
            return CertificateBundle(
                environment=request.environment,
                profile=profile.name,
                identity=identity,
                dns_names=dns_names,
                certificate_pem=certificate,
                certificate_chain_pem=certificate + ca_pem,
                ca_certificate_pem=ca_pem,
            )

    def _resolve_identity(self, request: CertificateRequest) -> str | None:
        """Сверяет identity запроса с допустимым для профиля."""
        expected = request.expected_identity()
        stated = (request.identity or "").strip()
        if expected is None:
            if stated:
                raise PkiError(
                    f"profile {request.profile} carries no identity, but {stated!r} was requested"
                )
            return None
        if stated and stated != expected:
            raise PkiError(f"identity {stated!r} does not belong to profile {request.profile}")

        pattern = INSTANCE_IDENTITY if "/instance/" in expected else SERVICE_IDENTITY
        match = pattern.fullmatch(expected)
        if match is None:
            raise PkiError(f"malformed certificate identity: {expected!r}")
        # Identity обязан принадлежать запрошенной среде.
        if match.group("environment") != request.environment:
            raise PkiError("certificate identity does not belong to the requested environment")
        return expected

    def _resolve_dns_names(self, request: CertificateRequest) -> tuple[str, ...]:
        profile = request.resolved_profile()
        names = tuple(name.strip() for name in request.dns_names)
        if not profile.requires_dns:
            if names:
                raise PkiError(f"profile {profile.name} does not carry DNS names")
            return ()
        if not names:
            # Серверный сертификат обязан содержать проверяемый DNS SAN.
            raise PkiError(f"profile {profile.name} requires at least one DNS name")
        for name in names:
            if not HOST_NAME.fullmatch(name):
                raise PkiError(f"malformed DNS name: {name!r}")
        if profile.name == "agent-server":
            expected = agent_dns_name(request.environment, request.subject or "")
            if names != (expected,):
                raise PkiError(
                    f"agent-server certificate must carry exactly one DNS name {expected!r}"
                )
        return names

    @staticmethod
    def _require_declared_sans(csr_text: str, expected: tuple[str, ...]) -> None:
        """Требует точного совпадения SAN в CSR и выдаваемом сертификате."""
        declared = _subject_alt_names(csr_text)
        if declared != set(expected):
            raise PkiError(
                "CSR does not declare the certificate it is being issued: "
                f"declared {sorted(declared)}, issuing {sorted(expected)}"
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
            str(CA_VALIDITY_DAYS),
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


def _subject_alt_names(text: str) -> set[str]:
    """Извлекает SAN из текстового вывода OpenSSL."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if _SAN_HEADER not in line:
            continue
        indent = len(line) - len(line.lstrip())
        collected: list[str] = []
        for candidate in lines[index + 1 :]:
            if not candidate.strip():
                break
            if len(candidate) - len(candidate.lstrip()) <= indent:
                break
            collected.append(candidate.strip())
        return {entry.strip() for entry in ", ".join(collected).split(",") if entry.strip()}
    return set()
