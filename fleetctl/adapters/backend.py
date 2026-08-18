"""Client for the vendored ManifestService contract.

The deployment pipeline compiles a complete manifest snapshot and durably
allocates its revision long before anything is sent. This adapter is the last
step of that boundary and nothing more: it hands one already-compiled request to
the backend and reports what the backend said about it.

grpcio and protobuf are imported lazily. Every other fleetctl command — validate,
render, plan, the whole check suite — runs without them, and an operator
workstation that never sends a manifest should not have to install a gRPC stack
to compile one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


class BackendCallError(Exception):
    """The manifest could not be handed over, or was refused."""


# APPLIED and IDEMPOTENT are both successful deployment-boundary results:
# re-sending one revision with the same canonical digest is defined as a no-op,
# and a pipeline that retried must not read that as a failure.
_ACCEPTED_RESULTS = ("MANIFEST_APPLY_RESULT_APPLIED", "MANIFEST_APPLY_RESULT_IDEMPOTENT")


@dataclass(frozen=True, slots=True)
class BackendEndpoint:
    """Where the backend is, and what proves both sides are who they claim."""

    target: str
    tls_server_name: str
    client_certificate: Path
    client_private_key: Path
    certificate_authority: Path

    def require_readable_material(self) -> None:
        missing = [
            str(path)
            for path in (
                self.client_certificate,
                self.client_private_key,
                self.certificate_authority,
            )
            if not path.is_file()
        ]
        if missing:
            raise BackendCallError(
                "manifest-writer TLS material is missing: " + ", ".join(sorted(missing))
            )


def apply_fleet_manifest(
    request: dict[str, Any],
    *,
    endpoint: BackendEndpoint,
    timeout_seconds: int = 60,
) -> str:
    """Send one compiled manifest and return the backend's result name."""

    endpoint.require_readable_material()
    grpc, manifest_pb2, manifest_pb2_grpc, parse_dict = _load_grpc_runtime()

    try:
        message = parse_dict(request, manifest_pb2.ApplyFleetManifestRequest())
    except Exception as exc:  # a malformed request must never reach the wire
        raise BackendCallError(f"compiled manifest does not match the contract: {exc}") from exc

    credentials = grpc.ssl_channel_credentials(
        root_certificates=endpoint.certificate_authority.read_bytes(),
        private_key=endpoint.client_private_key.read_bytes(),
        certificate_chain=endpoint.client_certificate.read_bytes(),
    )
    # The backend is reached at its overlay address while its certificate is
    # issued for a name that has no DNS anywhere. Overriding the name checked
    # against the certificate is what keeps verification on: the alternative in
    # practice is somebody disabling it.
    options = [("grpc.ssl_target_name_override", endpoint.tls_server_name)]

    with grpc.secure_channel(endpoint.target, credentials, options=options) as channel:
        stub = manifest_pb2_grpc.ManifestServiceStub(channel)
        try:
            response = stub.ApplyFleetManifest(message, timeout=timeout_seconds)
        except grpc.RpcError as exc:
            code = exc.code().name if hasattr(exc, "code") and exc.code() else "UNKNOWN"
            detail = exc.details() if hasattr(exc, "details") else ""
            raise BackendCallError(
                f"ApplyFleetManifest failed at {endpoint.target}: {code}: {detail}"
            ) from exc

    result = manifest_pb2.ManifestApplyResult.Name(response.result)
    if result not in _ACCEPTED_RESULTS:
        raise BackendCallError(f"backend refused the manifest: {result}")
    return result


def _load_grpc_runtime() -> tuple[Any, Any, Any, Any]:
    try:
        import grpc
        from google.protobuf.json_format import ParseDict

        from fleetctl.gen import manifest_pb2, manifest_pb2_grpc
    except ImportError as exc:
        raise BackendCallError(
            "sending a manifest needs grpcio and protobuf, which this interpreter "
            f"does not have: {exc}"
        ) from exc
    return grpc, manifest_pb2, manifest_pb2_grpc, ParseDict
