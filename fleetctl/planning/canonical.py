"""Каноническое желаемое состояние для сравнения и хеширования."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fleetctl.model import DesiredState, common_values


def canonical_state(state: DesiredState) -> dict[str, Any]:
    return {
        "common": common_values(state.environment_common),
        "environment": {
            "id": state.environment.object_id,
            "dns_zone": state.environment.dns_zone,
            "management_network": state.environment.management_network,
            "backend_endpoint": state.environment.backend_endpoint,
            "secret_kv": state.environment.secret_kv,
            "secret_pki": state.environment.secret_pki,
            "control": _control_values(state),
        },
        "fleet_ids": dict(sorted(state.fleet_ids.items())),
        "fleets": [
            {
                "id": fleet.object_id,
                "entries": list(fleet.entries),
                "exits": list(fleet.exits),
                "bridges": [
                    {
                        "routing_key": bridge.routing_key,
                        "entry": bridge.entry,
                        "exit": bridge.exit,
                        "display_name": bridge.display_name,
                        "service_credential_ref": bridge.service_credential_ref,
                    }
                    for bridge in sorted(fleet.bridges, key=lambda item: item.routing_key)
                ],
            }
            for fleet in sorted(state.fleets, key=lambda item: item.object_id)
        ],
        "nodes": [
            {
                "id": node.object_id,
                "role": node.role,
                "region": node.region,
                "display_name": node.display_name,
                "hostname": node.hostname,
                "public_port": node.public_port,
                "transport": node.transport,
                "flow": node.flow,
                "fingerprint": node.fingerprint,
                "server_name": node.server_name,
                "reality_public_key": node.reality_public_key,
                "reality_short_id": node.reality_short_id,
                "private_key_ref": node.private_key_ref,
                "mask_certificate_ref": node.mask_certificate_ref,
                "mask_private_key_ref": node.mask_private_key_ref,
                "common": common_values(state.common_for_node(node.object_id)),
            }
            for node in sorted(state.nodes, key=lambda item: item.object_id)
        ],
        "instances": [
            {
                "id": instance.object_id,
                "logical_node": instance.logical_node,
                "target_state": instance.target_state,
                "public_address": instance.public_address,
                "bandwidth_profile": instance.bandwidth_profile,
                "provider_name": instance.provider_name,
                "provider_resource_id": instance.provider_resource_id,
            }
            for instance in sorted(state.instances, key=lambda item: item.object_id)
        ],
    }


def _control_values(state: DesiredState) -> dict[str, Any] | None:
    control = state.environment.control
    if control is None:
        return None
    return {
        "backend_source_git_sha": control.backend_source_git_sha,
        "backend_image": {
            "repository": control.backend_image.repository,
            "digest": control.backend_image.digest,
        },
        "migration_image": {
            "repository": control.migration_image.repository,
            "digest": control.migration_image.digest,
        },
        "postgres_image": {
            "repository": control.postgres_image.repository,
            "digest": control.postgres_image.digest,
        },
        "postgres_major_version": control.postgres_major_version,
        "postgres_database": control.postgres_database,
        "postgres_owner_user": control.postgres_owner_user,
        "postgres_runtime_user": control.postgres_runtime_user,
        "backup_required": control.backup_required,
        "external_backup_command_argv": list(control.external_backup_command_argv),
        "customer_access_writers": list(control.customer_access_writers),
        "customer_access_readers": list(control.customer_access_readers),
        # Публичная точка хаба проецируется здесь, иначе её смена не меняла бы
        # канонический дайджест: план сообщил бы «изменений нет», а
        # `dns-plan.json` уже был бы другим. Ключ присутствует и когда точка не
        # объявлена — у baseline прошлой выкатки он читается как `null`.
        "public_hostname": control.public_hostname,
        "public_address": control.public_address,
    }


def canonical_digest(state: DesiredState) -> str:
    payload = json.dumps(canonical_state(state), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"
