"""Минимальный inventory публичных адресов для первого бутстрапа VPS."""

from __future__ import annotations

from typing import Any

from fleetctl.model import DesiredState


def compile_bootstrap_inventory(state: DesiredState) -> dict[str, Any]:
    hosts: dict[str, dict[str, Any]] = {}
    for instance in sorted(state.instances, key=lambda item: item.object_id):
        if instance.target_state == "retired":
            continue
        hosts[instance.object_id] = {
            "ansible_host": instance.public_address,
            # 22 у подавляющего большинства машин, и раньше он был здесь
            # неявным — просто не задавался. Неявный порт молча ломал первый
            # контакт там, где провайдер отдаёт машину с другим, и разбираться
            # приходилось с таймаутом SSH вместо объявления.
            "ansible_port": instance.bootstrap_port,
            "ansible_user": "root",
            "spiritvpn_connection_phase": "bootstrap",
            "spiritvpn_node_plan_file": f"node-plans/{instance.object_id}.json",
        }
    return {"all": {"children": {"spiritvpn_bootstrap": {"hosts": hosts}}}}
