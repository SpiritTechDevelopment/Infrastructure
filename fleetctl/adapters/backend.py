"""Клиент вендоренного контракта ManifestService.

Передаёт бэкенду готовый снимок манифеста. gRPC-зависимости загружаются только
при отправке, поэтому остальные команды fleetctl работают без них.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


class BackendCallError(Exception):
    """Манифест не удалось передать либо бэкенд его отклонил."""


# APPLIED и IDEMPOTENT означают успешную передачу манифеста.
_ACCEPTED_RESULTS = ("MANIFEST_APPLY_RESULT_APPLIED", "MANIFEST_APPLY_RESULT_IDEMPOTENT")


@dataclass(frozen=True, slots=True)
class BackendEndpoint:
    """Адрес бэкенда и данные взаимной проверки сторон."""

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
    """Отправляет манифест и возвращает результат бэкенда."""

    endpoint.require_readable_material()
    grpc, manifest_pb2, manifest_pb2_grpc, parse_dict = _load_grpc_runtime()

    try:
        message = parse_dict(request, manifest_pb2.ApplyFleetManifestRequest())
    except Exception as exc:  # Некорректный запрос нельзя отправлять в сеть.
        raise BackendCallError(f"compiled manifest does not match the contract: {exc}") from exc

    credentials = grpc.ssl_channel_credentials(
        root_certificates=endpoint.certificate_authority.read_bytes(),
        private_key=endpoint.client_private_key.read_bytes(),
        certificate_chain=endpoint.client_certificate.read_bytes(),
    )
    # Бэкенд доступен по overlay-адресу, а сертификат проверяется по имени без DNS.
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
