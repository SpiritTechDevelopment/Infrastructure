"""Выпуск сертификатов с именами из проверенного желаемого состояния."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fleetctl.compiler.addressing import agent_certificate_identity, agent_tls_server_name
from fleetctl.model import DesiredState

from .keys import generate_key_and_csr
from .local_ca import DEFAULT_VALIDITY_DAYS, LocalCertificateAuthorityAdapter
from .model import CertificateRequest, PkiError

# Профили процессов выпускаются оператором; ключ agent-server остаётся на ноде.
CONTROL_PROFILES = ("backend-server", "backend-client", "manifest-writer", "customer-service")

# Профиль -> компонент, имена файлов для сертификата и ключа, имена якорей CA.
# Имена совпадают с `control_backend_required_files` и
# `control_bot_required_files` в роли control_runtime: это одни и те же файлы,
# названные один раз выпуском и один раз проводкой.
VAULT_FILES: dict[str, tuple[str, dict[str, str], tuple[str, ...]]] = {
    "backend-server": (
        "backend",
        {"certificate": "server.crt", "private_key": "server.key"},
        ("clients-ca.crt", "agents-ca.crt"),
    ),
    "backend-client": (
        "backend",
        {"certificate": "agent-client.crt", "private_key": "agent-client.key"},
        ("clients-ca.crt", "agents-ca.crt"),
    ),
    "customer-service": (
        "bot",
        {"certificate": "client.crt", "private_key": "client.key"},
        ("server-ca.crt",),
    ),
}

# Один корень на окружение означает, что оба якоря сегодня держат одни и те же
# байты. Имена оставлены разными, чтобы корни можно было развести позже без
# смены схемы, и оба заполняются каждым выпуском.


def issue_control_certificate(
    state: DesiredState,
    profile: str,
    *,
    ca_state: Path,
    output: Path,
    validity_days: int = DEFAULT_VALIDITY_DAYS,
) -> dict[str, Any]:
    if profile not in CONTROL_PROFILES:
        known = ", ".join(CONTROL_PROFILES)
        raise PkiError(f"{profile!r} is not issued here; control profiles: {known}")

    environment = state.environment.object_id
    if profile == "backend-server":
        host = state.environment.backend_endpoint.rsplit(":", 1)[0]
        dns_names: tuple[str, ...] = (host,)
        common_name = host
    else:
        dns_names = ()
        common_name = profile

    request = CertificateRequest(
        environment=environment,
        profile=profile,
        csr_pem=b"",
        dns_names=dns_names,
    )
    identity = request.expected_identity()
    _require_authorised(state, profile, identity)

    # Каталог с приватным ключом доступен только владельцу.
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output, 0o700)
    csr_pem = generate_key_and_csr(
        output / f"{profile}.key",
        output / f"{profile}.csr",
        common_name=common_name,
        subject_alt_names=request.subject_alt_names(),
    )
    bundle = LocalCertificateAuthorityAdapter(ca_state, validity_days).sign(
        CertificateRequest(
            environment=environment,
            profile=profile,
            csr_pem=csr_pem,
            identity=identity,
            dns_names=dns_names,
        )
    )

    certificate_path = output / f"{profile}.crt"
    ca_path = output / "ca.crt"
    certificate_path.write_bytes(bundle.certificate_pem)
    ca_path.write_bytes(bundle.ca_certificate_pem)
    return {
        "environment": environment,
        "profile": profile,
        "identity": bundle.identity,
        "dns_names": list(bundle.dns_names),
        "validity_days": validity_days,
        "files": {
            "private_key": str(output / f"{profile}.key"),
            "certificate": str(certificate_path),
            "ca_certificate": str(ca_path),
        },
        "vault": _vault_targets(state, profile),
    }


def sign_agent_certificate(
    state: DesiredState,
    instance_id: str,
    *,
    ca_state: Path,
    csr_pem: bytes,
    output: Path,
    validity_days: int = DEFAULT_VALIDITY_DAYS,
) -> dict[str, Any]:
    environment = state.environment.object_id
    instance = next(
        (item for item in state.instances if item.object_id == instance_id),
        None,
    )
    if instance is None:
        # Необъявленный инстанс не может получить сертификат.
        raise PkiError(f"instance {instance_id!r} is not declared in {environment}")

    identity = agent_certificate_identity(state.environment, instance)
    server_name = agent_tls_server_name(state.environment, instance)
    bundle = LocalCertificateAuthorityAdapter(ca_state, validity_days).sign(
        CertificateRequest(
            environment=environment,
            profile="agent-server",
            csr_pem=csr_pem,
            identity=identity,
            dns_names=(server_name,),
            subject=instance_id,
        )
    )

    output.mkdir(parents=True, exist_ok=True)
    chain_path = output / f"{instance_id}-chain.pem"
    ca_path = output / "ca.crt"
    # Агенту нужна полная цепочка leaf + CA.
    chain_path.write_bytes(bundle.certificate_chain_pem)
    ca_path.write_bytes(bundle.ca_certificate_pem)
    return {
        "environment": environment,
        "profile": "agent-server",
        "instance_id": instance_id,
        "identity": bundle.identity,
        "dns_names": list(bundle.dns_names),
        "validity_days": validity_days,
        "files": {
            "certificate_chain": str(chain_path),
            "ca_certificate": str(ca_path),
        },
        "install": (
            "pass the chain to roles/pki_agent as pki_agent_certificate_chain; "
            "the private key stays on the node"
        ),
    }


def _require_authorised(state: DesiredState, profile: str, identity: str | None) -> None:
    """Проверяет, что identity клиентского сертификата разрешён control plane."""
    control = state.environment.control
    if control is None or profile != "customer-service" or identity is None:
        return
    authorised = set(control.customer_access_writers) | set(control.customer_access_readers)
    if identity not in authorised:
        raise PkiError(
            f"{identity} appears in neither customer_access_writers nor "
            "customer_access_readers; the backend would reject it with PermissionDenied"
        )


def _vault_targets(state: DesiredState, profile: str) -> dict[str, Any]:
    """Куда оператору положить выпущенное.

    Адрес больше не объявляется в топологии — он выводится из соглашения: поля
    объекта `.../files` становятся файлами в защищённом каталоге, а имя поля —
    именем файла. Те же имена перечислены в `control_runtime` как проводка, и
    расхождение между ними означало бы выпуск, который выкатка не найдёт.
    """
    control = state.environment.control
    if control is None:
        return {}
    if profile not in VAULT_FILES:
        return {}
    component, artifacts, anchor_names = VAULT_FILES[profile]
    if component == "bot" and control.bot is None:
        return {}
    path = f"kv/{state.environment.object_id}/control/{component}/files"
    targets: dict[str, Any] = {
        artifact: f"{path}#{name}" for artifact, name in artifacts.items()
    }
    # Один и тот же PEM ложится в оба якоря; перечисление делает дублирование
    # явным, вместо того чтобы оператор помнил о нём сам.
    targets["ca_certificate"] = [f"{path}#{name}" for name in anchor_names]
    return targets
