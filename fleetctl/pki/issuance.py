"""Issuance ceremonies: desired state decides the names, the CA only signs them.

Every name here is read from validated desired state or from the same helpers
the compiler uses, never typed by the operator. That is the whole point of
routing issuance through fleetctl rather than through an openssl recipe in a
runbook: the host name in the certificate and the host name the peer verifies
come out of one function.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fleetctl.compiler.addressing import agent_certificate_identity, agent_tls_server_name
from fleetctl.model import DesiredState

from .keys import generate_key_and_csr
from .local_ca import DEFAULT_VALIDITY_DAYS, LocalCertificateAuthorityAdapter
from .model import CertificateRequest, PkiError

# Profiles that belong to a process rather than to a machine, and so are created
# wherever the operator runs this command. agent-server is deliberately absent:
# its key is generated on the node by roles/pki_agent and only its CSR travels.
CONTROL_PROFILES = ("backend-server", "backend-client", "manifest-writer", "customer-service")

# Which declared secret each artifact belongs in. Read back from the environment
# rather than reconstructed, so a renamed path in desired state cannot drift
# from what this command tells the operator to fill.
VAULT_FIELDS = {
    "backend-server": {
        "certificate": "grpc_tls_certificate_ref",
        "private_key": "grpc_tls_private_key_ref",
    },
    "backend-client": {
        "certificate": "agent_tls_certificate_ref",
        "private_key": "agent_tls_private_key_ref",
    },
}

# The bot keeps its own secrets under spec.control.bot, so its artifacts are
# looked up there rather than beside the backend's. Same rule as above: the
# operator is told a path read back from desired state, not one retyped.
BOT_VAULT_FIELDS = {
    "customer-service": {
        "certificate": "grpc_client_certificate_ref",
        "private_key": "grpc_client_private_key_ref",
    },
}

# One root per environment means both trust anchors hold the same bytes today.
# The fields stay separate so the roots can be split later without a schema
# change, and both are filled from every issuance.
CA_FIELDS = ("grpc_tls_client_ca_ref", "agent_tls_ca_ref")

# The bot verifies the backend's server certificate, so it needs the same root
# under its own reference.
BOT_CA_FIELDS = ("grpc_server_ca_ref",)


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

    # 0700: this directory receives a private key. The key itself is 0600, but a
    # world-readable directory holding one is the wrong default to hand an
    # operator who will later copy things out of it.
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
        # Refusing here is the same rule the backend applies at runtime: an
        # instance absent from desired state gets nothing, certificate or not.
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
    # The chain, not the leaf: compiled_runtime mounts this one file as both the
    # served certificate and SPIRIT_GRPC_TLS_CLIENT_CA_FILE, so the root has to
    # travel with it.
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
    """A client certificate no role list names is a cert that authorises nothing.

    Only checkable once the environment declares spec.control; before that the
    lists do not exist and issuance is still legitimate.
    """
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
    control = state.environment.control
    if control is None:
        return {}
    if profile in BOT_VAULT_FIELDS:
        if control.bot is None:
            return {}
        fields = BOT_VAULT_FIELDS[profile]
        references = control.bot.secret_refs
        ca_fields: tuple[str, ...] = BOT_CA_FIELDS
    elif profile in VAULT_FIELDS:
        fields = VAULT_FIELDS[profile]
        references = control.secret_refs
        ca_fields = CA_FIELDS
    else:
        return {}
    targets: dict[str, Any] = {
        artifact: references[field]
        for artifact, field in fields.items()
        if field in references
    }
    # The same PEM goes into both anchors; listing them makes the duplication
    # explicit rather than something an operator has to remember.
    anchors = [references[field] for field in ca_fields if field in references]
    if anchors:
        targets["ca_certificate"] = anchors
    return targets
