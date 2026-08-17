"""Deterministic local control-plane deployment projection."""

from __future__ import annotations

from typing import Any

from fleetctl.model import DesiredState

from .addressing import CONTROL_BACKEND_METRICS_PORT, control_management_address


def compile_control_plan(state: DesiredState) -> dict[str, Any] | None:
    control = state.environment.control
    if control is None:
        return None
    backend_host, backend_port_text = state.environment.backend_endpoint.rsplit(":", 1)
    management_address = control_management_address(state.environment)
    environment = state.environment.object_id
    root = f"/opt/spiritvpn/control/{environment}"
    state_root = f"/var/lib/spiritvpn/control/{environment}"
    return {
        "_notice": "GENERATED — DO NOT EDIT",
        "schema_version": 1,
        "environment": environment,
        "paths": {
            "root": root,
            "secrets": f"{root}/secrets",
            "postgres_data": f"{state_root}/postgres",
            "prometheus_data": f"{state_root}/prometheus",
            "backups": f"{state_root}/backups",
        },
        "network": {
            "management_address": management_address,
            "management_network": state.environment.management_network,
            "backend_endpoint": state.environment.backend_endpoint,
            "backend_tls_server_name": backend_host,
            "backend_host_port": int(backend_port_text),
            "backend_container_port": 8443,
            "backend_metrics_port": CONTROL_BACKEND_METRICS_PORT,
        },
        "backend": {
            "source_git_sha": control.backend_source_git_sha,
            "image": control.backend_image.image,
            "migration_image": control.migration_image.image,
            "database_max_connections": 10,
            "log_level": "info",
            "manifest_writer_identity": (
                f"spiffe://spiritvpn/{environment}/service/manifest-writer"
            ),
            "agent_client_identity": f"spiffe://spiritvpn/{environment}/service/backend",
            "customer_access_writers": list(control.customer_access_writers),
            "customer_access_readers": list(control.customer_access_readers),
        },
        "postgres": {
            "image": control.postgres_image.image,
            "major_version": control.postgres_major_version,
            "database": control.postgres_database,
            "owner_user": control.postgres_owner_user,
            "runtime_user": control.postgres_runtime_user,
            "backup_required": control.backup_required,
        },
        "observability": _observability_projection(state),
        "secret_refs": dict(sorted(control.secret_refs.items())),
    }


def _observability_projection(state: DesiredState) -> dict[str, Any]:
    """What this environment expects of the shared collector.

    The collector itself is one instance for every environment, deployed by the
    platform contour beside Vault, so its image and retention are platform
    values rather than environment ones — a shared TSDB cannot hold two
    retentions. Only the scrape cadence is projected here, and the control role
    asserts it against the deployed collector so desired state cannot describe
    a cadence that nothing applies.
    """
    return {
        "scrape_interval_seconds": state.environment_common.observability.scrape_interval_seconds,
    }
