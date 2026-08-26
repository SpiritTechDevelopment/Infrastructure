"""Интерфейс CA машинных сертификатов, изолированных по средам."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol


class PkiError(Exception):
    pass


ENVIRONMENTS = ("develop", "prod")

# Identity формирует компилятор; шаблоны лишь проверяют допустимый формат.
INSTANCE_IDENTITY = re.compile(
    r"^spiffe://spiritvpn/(?P<environment>develop|prod)/instance/(?P<name>[a-z0-9-]{1,63})$"
)
SERVICE_IDENTITY = re.compile(
    r"^spiffe://spiritvpn/(?P<environment>develop|prod)/service/(?P<name>[a-z0-9-]{1,63})$"
)

# DNS SAN не допускает wildcard, чтобы один ключ не представлял всю зону.
HOST_NAME = re.compile(r"^(?![0-9.]+$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")


@dataclass(frozen=True, slots=True)
class CertificateProfile:
    """Описывает допустимые назначения и identity сертификата peer."""

    name: str
    extended_key_usage: tuple[str, ...]
    identity_path: str | None
    requires_dns: bool


# Ключи EC P-256 используются только для цифровой подписи в TLS 1.3.
KEY_USAGE = ("digitalSignature",)

PROFILES: dict[str, CertificateProfile] = {
    # Сертификат сервера агента не получает лишнее назначение clientAuth.
    "agent-server": CertificateProfile(
        name="agent-server",
        extended_key_usage=("serverAuth",),
        identity_path="instance/{subject}",
        requires_dns=True,
    ),
    # Серверный сертификат бэкенда проверяется только по DNS SAN.
    "backend-server": CertificateProfile(
        name="backend-server",
        extended_key_usage=("serverAuth",),
        identity_path=None,
        requires_dns=True,
    ),
    # Identity бэкенда для вызова агентов.
    "backend-client": CertificateProfile(
        name="backend-client",
        extended_key_usage=("clientAuth",),
        identity_path="service/backend",
        requires_dns=False,
    ),
    # Identity CI/CD для отправки манифеста бэкенду.
    "manifest-writer": CertificateProfile(
        name="manifest-writer",
        extended_key_usage=("clientAuth",),
        identity_path="service/manifest-writer",
        requires_dns=False,
    ),
    # Identity CustomerService для работы с доступами клиентов.
    "customer-service": CertificateProfile(
        name="customer-service",
        extended_key_usage=("clientAuth",),
        identity_path="service/customer-service",
        requires_dns=False,
    ),
}


def agent_dns_name(environment: str, instance_id: str) -> str:
    """Повторяет имя из compiler.addressing для независимой проверки CA."""
    return f"{instance_id}.agent.{environment}.internal"


@dataclass(frozen=True, slots=True)
class CertificateRequest:
    """Запрос подписи с identity и DNS-именами, проверяемыми CA."""

    environment: str
    profile: str
    csr_pem: bytes
    identity: str | None = None
    dns_names: tuple[str, ...] = ()
    # Subject нужен профилям с шаблоном {subject}, например agent-server.
    subject: str | None = None

    def resolved_profile(self) -> CertificateProfile:
        profile = PROFILES.get(self.profile)
        if profile is None:
            known = ", ".join(sorted(PROFILES))
            raise PkiError(f"unknown certificate profile {self.profile!r}; known profiles: {known}")
        return profile

    def expected_identity(self) -> str | None:
        """Возвращает единственный допустимый identity запроса."""
        profile = self.resolved_profile()
        if profile.identity_path is None:
            return None
        path = profile.identity_path
        if "{subject}" in path:
            if not self.subject:
                raise PkiError(f"profile {profile.name} requires a subject name")
            path = path.format(subject=self.subject)
        return f"spiffe://spiritvpn/{self.environment}/{path}"

    def subject_alt_names(self) -> tuple[str, ...]:
        """Возвращает SAN в порядке записи и сравнения."""
        names = [f"DNS:{name}" for name in self.dns_names]
        identity = self.expected_identity()
        if identity is not None:
            names.append(f"URI:{identity}")
        return tuple(names)


@dataclass(frozen=True, slots=True)
class CertificateBundle:
    environment: str
    profile: str
    identity: str | None
    dns_names: tuple[str, ...]
    # Leaf-сертификат для потребителей с отдельным trust anchor.
    certificate_pem: bytes
    # Цепочка leaf + CA для агента.
    certificate_chain_pem: bytes
    ca_certificate_pem: bytes


class CertificateAuthorityAdapter(Protocol):
    def sign(self, request: CertificateRequest) -> CertificateBundle: ...
