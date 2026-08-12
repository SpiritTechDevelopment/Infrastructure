"""Adapter boundary for environment-scoped machine certificate authorities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class PkiError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class CertificateRequest:
    environment: str
    identity: str
    csr_pem: bytes


@dataclass(frozen=True, slots=True)
class CertificateBundle:
    environment: str
    identity: str
    certificate_chain_pem: bytes
    ca_certificate_pem: bytes


class CertificateAuthorityAdapter(Protocol):
    def sign(self, request: CertificateRequest) -> CertificateBundle: ...
