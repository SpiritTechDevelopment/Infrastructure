"""Adapter boundary for environment-scoped machine certificate authorities."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol


class PkiError(Exception):
    pass


ENVIRONMENTS = ("develop", "prod")

# Identities are never invented here. Every string this module accepts is also
# produced by the compiler (fleetctl/compiler/addressing.py, control.py) and
# asserted again by the Ansible roles; the patterns below only reject shapes the
# verifying side could not match. A second source of truth for identity strings
# is exactly the failure this PKI exists to prevent.
INSTANCE_IDENTITY = re.compile(
    r"^spiffe://spiritvpn/(?P<environment>develop|prod)/instance/(?P<name>[a-z0-9-]{1,63})$"
)
SERVICE_IDENTITY = re.compile(
    r"^spiffe://spiritvpn/(?P<environment>develop|prod)/service/(?P<name>[a-z0-9-]{1,63})$"
)

# A DNS SAN is a host name, not a pattern: wildcards would let one leaked key
# answer for every name in the zone.
HOST_NAME = re.compile(r"^(?![0-9.]+$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")


@dataclass(frozen=True, slots=True)
class CertificateProfile:
    """What one kind of peer is allowed to be given.

    ``identity_path`` is the part of the SPIFFE name after the environment.
    ``None`` means the profile carries no URI SAN at all: identity in this
    system is a property of the *calling* side — every verification site
    (grpcsvc/auth.go, nodeagent/tls.go, grpcserver/tls.go) reads the SAN of the
    peer it is authorising — so giving a server key the same SPIFFE name as the
    client key that authenticates outbound would put one identity on two keys
    and make it unrevokable by reissue.
    """

    name: str
    extended_key_usage: tuple[str, ...]
    identity_path: str | None
    requires_dns: bool


# keyUsage is digitalSignature only. Every key here is EC P-256 and both peers
# require TLS 1.3, which has no RSA key transport, so keyEncipherment would
# describe an operation none of these certificates can perform.
KEY_USAGE = ("digitalSignature",)

PROFILES: dict[str, CertificateProfile] = {
    # The node's own server certificate, and nothing else. The agent never
    # dials anything over mTLS — its only outbound gRPC is to local Xray on the
    # loopback with insecure credentials — so clientAuth would grant a usage it
    # has no need of. That matters: with one root per environment, a leaf
    # carrying clientAuth chains successfully as a client anywhere in the trust
    # domain and is stopped only by the identity allow-list. Without it, Go
    # rejects the leaf during chain verification, one step earlier.
    "agent-server": CertificateProfile(
        name="agent-server",
        extended_key_usage=("serverAuth",),
        identity_path="instance/{subject}",
        requires_dns=True,
    ),
    # Terminates gRPC for manifest and customer-access clients. DNS SAN only:
    # control_runtime asserts it with `openssl x509 -checkhost`, and nothing
    # reads an identity from it.
    "backend-server": CertificateProfile(
        name="backend-server",
        extended_key_usage=("serverAuth",),
        identity_path=None,
        requires_dns=True,
    ),
    # The backend calling agents. This is the identity the agent allows in
    # SPIRIT_GRPC_ALLOWED_CLIENT_IDENTITIES.
    "backend-client": CertificateProfile(
        name="backend-client",
        extended_key_usage=("clientAuth",),
        identity_path="service/backend",
        requires_dns=False,
    ),
    # Infrastructure CI/CD calling the backend; SPIRIT_ROLE_MANIFEST_WRITER.
    "manifest-writer": CertificateProfile(
        name="manifest-writer",
        extended_key_usage=("clientAuth",),
        identity_path="service/manifest-writer",
        requires_dns=False,
    ),
    # CustomerService calling the backend; goes into
    # authorization.customer_access_writers / _readers. Not implemented yet, so
    # the identity is fixed here before anything depends on it: the same string
    # has to appear in desired state, and inventing it twice is how the two
    # drift.
    "customer-service": CertificateProfile(
        name="customer-service",
        extended_key_usage=("clientAuth",),
        identity_path="service/customer-service",
        requires_dns=False,
    ),
}


def agent_dns_name(environment: str, instance_id: str) -> str:
    """Mirror of compiler.addressing.agent_tls_server_name.

    Duplicated deliberately and asserted against the caller's value rather than
    substituted for it: the CA must be able to reject a certificate request
    whose DNS name and SPIFFE name describe different machines.
    """
    return f"{instance_id}.agent.{environment}.internal"


@dataclass(frozen=True, slots=True)
class CertificateRequest:
    """A signing request, fully named by the caller.

    The caller states the identity and the host names; the CA checks them
    against the profile and writes them itself. Nothing is copied out of the
    CSR — a CSR is proof of key possession and a declaration of intent, never
    the source of what gets signed.
    """

    environment: str
    profile: str
    csr_pem: bytes
    identity: str | None = None
    dns_names: tuple[str, ...] = ()
    # Only meaningful for profiles whose identity_path has a {subject}; the
    # instance id for agent-server.
    subject: str | None = None

    def resolved_profile(self) -> CertificateProfile:
        profile = PROFILES.get(self.profile)
        if profile is None:
            known = ", ".join(sorted(PROFILES))
            raise PkiError(f"unknown certificate profile {self.profile!r}; known profiles: {known}")
        return profile

    def expected_identity(self) -> str | None:
        """The one identity this request is allowed to carry, or None."""
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
        """The SAN entries, in the order they are written and compared."""
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
    # The leaf alone, for consumers that are handed a trust anchor separately —
    # the backend reads its certificate and its CA from different files.
    certificate_pem: bytes
    # Leaf followed by the CA. The agent needs this form: the same file is its
    # served chain and its client CA pool.
    certificate_chain_pem: bytes
    ca_certificate_pem: bytes


class CertificateAuthorityAdapter(Protocol):
    def sign(self, request: CertificateRequest) -> CertificateBundle: ...
