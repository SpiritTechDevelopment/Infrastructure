"""Чистая проекция публикуемых DNS-записей."""

from __future__ import annotations

import ipaddress
from typing import Any

from fleetctl.model import DesiredState


def compile_dns_plan(state: DesiredState) -> dict[str, Any]:
    nodes = {node.object_id: node for node in state.nodes}
    records: list[dict[str, Any]] = []
    control_record = _control_record(state)
    if control_record is not None:
        # Первой и вне сортировки по инстансам: у хаба нет инстанса, а порядок
        # записей обязан быть детерминированным — рендер сверяется побайтово.
        records.append(control_record)
    for instance in sorted(state.instances, key=lambda item: item.object_id):
        node = nodes[instance.logical_node]
        # Обе роли принимают клиентов; entry маршрутизирует их на связанный exit.
        if instance.target_state != "serving":
            continue
        common = state.common_for_node(node.object_id)
        address = ipaddress.ip_address(instance.public_address)
        records.append(
            {
                "id": node.object_id,
                "logical_node_id": node.object_id,
                "instance_id": instance.object_id,
                "name": node.hostname,
                "record_type": "A" if address.version == 4 else "AAAA",
                "value": instance.public_address,
                "ttl_seconds": common.networking.dns_ttl_seconds,
                "proxied": common.networking.dns_proxied,
            }
        )
    return {
        "_notice": "GENERATED — DO NOT EDIT",
        "schema_version": 1,
        "environment": state.environment.object_id,
        "zone": state.environment.dns_zone,
        "records": records,
    }


def _control_record(state: DesiredState) -> dict[str, Any] | None:
    """Публичное имя самого хаба — то, под которым он обслуживает управляющие
    поверхности контура.

    Не проксируется, как и записи нод: за именем стоит собственный хост, а не
    edge. TTL берётся из общей политики, чтобы переезд хаба стоил столько же,
    сколько переезд ноды.
    """

    control = state.environment.control
    if control is None or control.public_hostname is None or control.public_address is None:
        return None
    address = ipaddress.ip_address(control.public_address)
    return {
        "id": "control",
        "name": control.public_hostname,
        "record_type": "A" if address.version == 4 else "AAAA",
        "value": control.public_address,
        "ttl_seconds": state.environment_common.networking.dns_ttl_seconds,
        "proxied": False,
    }
