"""Детерминированная проекция развёртывания control plane."""

from __future__ import annotations

from typing import Any

from fleetctl.model import DesiredState

from .addressing import (
    CONTROL_BACKEND_METRICS_PORT,
    CONTROL_POSTGRES_METRICS_PORT,
    control_management_address,
)


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
            "postgres_metrics_port": CONTROL_POSTGRES_METRICS_PORT,
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
            "exporter_image": control.postgres_exporter_image.image,
            "major_version": control.postgres_major_version,
            "database": control.postgres_database,
            "owner_user": control.postgres_owner_user,
            "runtime_user": control.postgres_runtime_user,
            "backup_required": control.backup_required,
            "external_backup_command_argv": list(control.external_backup_command_argv),
        },
        "observability": _observability_projection(state),
        "bot": _bot_projection(state),
    }


def _bot_projection(state: DesiredState) -> dict[str, Any] | None:
    """Формирует часть control-плана для бота и вычисляет его публичные URL."""
    control = state.environment.control
    if control is None or control.bot is None:
        return None
    bot = control.bot
    environment = state.environment.object_id
    root = f"/opt/spiritvpn/control/{environment}/bot"
    public_origin = f"https://{bot.public_hostname}"
    settings: dict[str, Any] = {
        "client_identity": bot.client_identity,
        "friends_plan_fleet": bot.friends_plan_fleet,
        "friends_plan_fleet_id": state.fleet_ids[bot.friends_plan_fleet],
        # Mini App и ссылки подписки обслуживаются из одного origin.
        "subscription_base_url": public_origin,
        "mini_app_url": public_origin,
    }
    if bot.friends_plan_quota_bytes is not None:
        settings["friends_plan_quota_bytes"] = bot.friends_plan_quota_bytes
    if bot.friends_plan_duration_days is not None:
        settings["friends_plan_duration_days"] = bot.friends_plan_duration_days
    return {
        "source_git_sha": bot.source_git_sha,
        "image": bot.image.image,
        "paths": {
            "root": root,
            "secrets": f"{root}/secrets",
        },
        "postgres": {
            "database": bot.postgres_database,
            "owner_user": bot.postgres_owner_user,
            "runtime_user": bot.postgres_runtime_user,
        },
        "network": {
            # Имя из сертификата бэкенда резолвится в его управляющий адрес.
            "backend_target": state.environment.backend_endpoint,
            "http_port": 8080,
        },
        "ingress": {
            "tunnel_image": bot.tunnel_image.image,
            "hostname": bot.public_hostname,
            "public_origin": public_origin,
        },
        "settings": settings,
    }


def _observability_projection(state: DesiredState) -> dict[str, Any]:
    """Формирует ожидания среды от общего коллектора."""
    return {
        "scrape_interval_seconds": state.environment_common.observability.scrape_interval_seconds,
        # Общий Loki требует одинакового retention во всех средах.
        "log_retention_days": state.environment_common.observability.activity_retention_days,
    }
