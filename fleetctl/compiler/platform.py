"""Deterministic platform bootstrap artifacts."""

from __future__ import annotations

import json
from typing import Any

from fleetctl.model import DesiredState, Platform


class PlatformNotDeclared(Exception):
    pass


def require_platform(state: DesiredState) -> Platform:
    if state.platform is None:
        raise PlatformNotDeclared(
            f"{state.environment.object_id}: platform descriptor is missing under "
            f"desired/environments/{state.environment.object_id}/platform/"
        )
    return state.platform


def compile_platform_plan(state: DesiredState) -> dict[str, Any]:
    platform = require_platform(state)
    vault = state.environment_common.components.components["vault"]
    assert vault.digest is not None
    environment = state.environment.object_id
    return {
        "_notice": "GENERATED — DO NOT EDIT",
        "schema_version": 1,
        "environment": environment,
        "platform": {
            "id": platform.object_id,
            "public_address": platform.public_address,
            "provider": {
                "name": platform.provider_name,
                "resource_id": platform.provider_resource_id,
            },
            "ssh": {
                "bootstrap_user": platform.bootstrap_user,
                "runtime_user": platform.runtime_user,
                "host_key_fingerprints": list(platform.host_key_fingerprints),
            },
        },
        "vault": {
            "image": f"{vault.repository}@{vault.digest}",
            "api": {
                "bind_address": "127.0.0.1",
                "port": platform.vault_api_port,
                "tls_server_name": platform.vault_tls_server_name,
            },
            "cluster": {
                "bind_address": "127.0.0.1",
                "port": platform.vault_cluster_port,
            },
            "storage": {"type": "raft", "node_id": platform.object_id},
            "tls": {
                "certificate_ref": platform.vault_tls_certificate_ref,
                "private_key_ref": platform.vault_tls_private_key_ref,
            },
            "mounts": {
                "kv": state.environment.secret_kv,
                "pki": state.environment.secret_pki,
            },
        },
        "github_actions": {
            "repository": platform.github_repository,
            "environment": platform.github_environment,
            "oidc": {
                "issuer": "https://token.actions.githubusercontent.com",
                "audience": platform.github_oidc_audience,
                "bound_subject": (
                    f"repo:{platform.github_repository}:environment:{platform.github_environment}"
                ),
            },
            "transport": {
                "kind": "ssh-tunnel",
                "remote_host": "127.0.0.1",
                "remote_port": platform.vault_api_port,
                "host_key_pinning_required": True,
            },
        },
        "operator_gates": [
            "verify-ssh-host-key",
            "supply-reviewed-operator-keys",
            "supply-vault-tls-material",
            "vault-operator-init",
            "store-recovery-material-outside-vault",
            "vault-operator-unseal",
            "configure-github-oidc",
            "revoke-bootstrap-token",
        ],
        "automation_boundary": {
            "bootstrap_installs_uninitialized_vault": True,
            "vault_init_is_automatic": False,
            "vault_unseal_is_automatic": False,
            "recovery_material_is_generated_or_stored_by_fleetctl": False,
            "deployment_refs_are_updated": False,
        },
    }


def compile_platform_inventories(state: DesiredState) -> dict[str, dict[str, Any]]:
    platform = require_platform(state)
    common = {
        "ansible_host": platform.public_address,
        "spiritvpn_platform_plan_file": "platform-plan.json",
    }
    bootstrap = {
        "all": {
            "children": {
                "spiritvpn_platform_bootstrap": {
                    "hosts": {
                        platform.object_id: {
                            **common,
                            "ansible_user": platform.bootstrap_user,
                            "spiritvpn_connection_phase": "bootstrap",
                        }
                    }
                }
            }
        }
    }
    runtime = {
        "all": {
            "children": {
                "spiritvpn_platform": {
                    "hosts": {
                        platform.object_id: {
                            **common,
                            "ansible_user": platform.runtime_user,
                            "spiritvpn_connection_phase": "runtime",
                        }
                    }
                }
            }
        }
    }
    return {"bootstrap": bootstrap, "runtime": runtime}


def render_platform_files(state: DesiredState) -> dict[str, bytes]:
    inventories = compile_platform_inventories(state)
    values = {
        "platform-plan.json": compile_platform_plan(state),
        "platform-bootstrap-inventory.json": inventories["bootstrap"],
        "platform-inventory.json": inventories["runtime"],
    }
    return {
        name: (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        for name, value in sorted(values.items())
    }
