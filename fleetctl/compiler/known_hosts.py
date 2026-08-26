"""Проекция SSH host key для бутстрапа и штатного подключения.

Ключ объявляется в желаемом состоянии и не обнаруживается через `ssh-keyscan`.
"""

from __future__ import annotations

from fleetctl.model import DesiredState

from .addressing import management_address


HEADER = (
    "# GENERATED — DO NOT EDIT\n"
    "# Rendered by fleetctl from desired state; edit the instance declaration instead.\n"
)

# Типы ключей синхронизированы со схемой instance и проверяются тестом.
HOST_KEY_TYPES = (
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "ssh-rsa",
)


class KnownHostsError(Exception):
    """У доступной машины не объявлен SSH host key."""


def compile_known_hosts(state: DesiredState) -> str:
    nodes = {node.object_id: node for node in state.nodes}
    lines = [HEADER]
    for instance in sorted(state.instances, key=lambda item: item.object_id):
        # Retired-инстанс больше не должен оставаться доверенным.
        if instance.target_state == "retired":
            continue
        # Проверка только доступных инстансов сохраняет совместимость со старыми baseline.
        if not instance.ssh_host_key:
            raise KnownHostsError(
                f"instance {instance.object_id} is reachable but declares no ssh_host_key; "
                "declare it rather than trusting the host on first contact"
            )
        node = nodes[instance.logical_node]
        patterns = [
            # Бутстрап подключается к публичному адресу чистой VPS.
            host_pattern(instance.public_address, 22),
            # Штатное подключение идёт через оверлей с тем же host key.
            host_pattern(
                management_address(state.environment, node, instance),
                state.common_for_node(node.object_id).networking.ssh_port,
            ),
        ]
        lines.append(f"{','.join(dict.fromkeys(patterns))} {instance.ssh_host_key}\n")
    return "".join(lines)


def host_pattern(address: str, port: int) -> str:
    """Формирует host pattern с учётом нестандартного SSH-порта."""

    return address if port == 22 else f"[{address}]:{port}"
