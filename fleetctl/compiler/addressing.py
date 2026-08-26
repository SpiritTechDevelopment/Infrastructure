"""Детерминированные значения из желаемого состояния."""

from __future__ import annotations

import ipaddress

from fleetctl.model import Environment, Instance, LogicalNode


ROLE_SUBNET = {"entry": 1, "exit": 2}

# HTTP-порт бэкенда без TLS доступен только в управляющем оверлее.
CONTROL_BACKEND_METRICS_PORT = 8080

# Экспортер PostgreSQL публикует метрики только в управляющем оверлее.
CONTROL_POSTGRES_METRICS_PORT = 9187

# Loki принимает логи нод через управляющий оверлей.
PLATFORM_LOKI_PORT = 3100


def control_management_address(environment: Environment) -> str:
    """Возвращает первый адрес сети, принадлежащий управляющему хабу."""
    network = ipaddress.ip_network(environment.management_network, strict=True)
    return str(network.network_address + 1)


def instance_slot(instance: Instance) -> int:
    return int(instance.object_id.rsplit("-", 1)[1])


def management_address(environment: Environment, node: LogicalNode, instance: Instance) -> str:
    network = ipaddress.ip_network(environment.management_network, strict=True)
    first, second, _, _ = network.network_address.packed
    return f"{first}.{second}.{ROLE_SUBNET[node.role]}.{10 + instance_slot(instance)}"


def agent_endpoint(environment: Environment, node: LogicalNode, instance: Instance, port: int) -> str:
    return f"{management_address(environment, node, instance)}:{port}"


def agent_tls_server_name(environment: Environment, instance: Instance) -> str:
    return f"{instance.object_id}.agent.{environment.object_id}.internal"


def agent_certificate_identity(environment: Environment, instance: Instance) -> str:
    return f"spiffe://spiritvpn/{environment.object_id}/instance/{instance.object_id}"
