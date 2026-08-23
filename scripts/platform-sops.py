#!/usr/bin/env python3
"""Decrypt and use the SOPS-sealed one-host platform bootstrap bundle."""

from __future__ import annotations

import argparse
import base64
import binascii
import copy
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_KEYS = {"apiVersion", "kind", "inventory", "known_hosts", "vars"}
EXPECTED_VARIABLE_KEYS = {
    "platform_alertmanager_telegram_bot_token",
    "platform_alertmanager_telegram_chat_id",
    "platform_alertmanager_telegram_thread_id",
    "platform_fail2ban_ignore_cidrs",
    "platform_github_ssh_keys",
    # Управляющий оверлей. Набор ключей сверяется на точное совпадение, поэтому
    # эти три обязаны появиться в бандле тем же коммитом, что и здесь: код
    # впереди бандла роняет выкатку на «unexpected key set», бандл впереди кода
    # — тоже.
    "platform_netbird_hostname",
    "platform_netbird_network",
    "platform_netbird_owner_email",
    "platform_operator_ssh_public_keys",
    "platform_runner",
    "platform_ssh_allowed_cidrs",
    "platform_vault_node_id",
    "platform_vault_tls_server_name",
    "platform_wireguard_environment_networks",
    "platform_wireguard_hub_addresses",
    "platform_wireguard_hub_public_key",
    "platform_wireguard_interface",
    "platform_wireguard_listen_port",
    "platform_wireguard_mtu",
    "platform_wireguard_operator_peers",
    "platform_wireguard_runner_peers",
}

RUNTIME_VARIABLE_KEYS = EXPECTED_VARIABLE_KEYS - {"platform_runner"}

TRANSITIONAL_RUNTIME_DEFAULTS = {
    "platform_wireguard_hub_public_key": "",
    "platform_wireguard_runner_peers": [],
}


class PlatformBundleError(Exception):
    """Raised when protected platform input cannot be used safely."""


def _require_command(command: str) -> str:
    resolved = shutil.which(command)
    if resolved is None:
        raise PlatformBundleError(f"required command is unavailable: {command}")
    return resolved


def _yaml_mapping(value: str, field: str) -> dict[str, Any]:
    try:
        document = yaml.safe_load(value)
    except yaml.YAMLError as exc:
        raise PlatformBundleError(f"decrypted {field} is invalid YAML") from exc
    if not isinstance(document, dict):
        raise PlatformBundleError(f"decrypted {field} must be a YAML mapping")
    return document


def _validate_cidrs(value: Any, field: str, *, require_one: bool) -> None:
    if not isinstance(value, list) or (require_one and not value):
        raise PlatformBundleError(f"{field} must be a{' non-empty' if require_one else ''} list")
    for item in value:
        if not isinstance(item, str):
            raise PlatformBundleError(f"{field} must contain strings")
        try:
            network = ipaddress.ip_network(item, strict=True)
        except ValueError as exc:
            raise PlatformBundleError(f"{field} contains an invalid canonical CIDR") from exc
        if network.prefixlen == 0:
            raise PlatformBundleError(f"{field} must not allow the entire internet")


def _validate_ssh_public_key(value: Any, field: str) -> None:
    if not isinstance(value, str):
        raise PlatformBundleError(f"{field} must contain SSH public-key strings")
    fields = value.split()
    if len(fields) < 2 or fields[0] not in {"ssh-ed25519", "ecdsa-sha2-nistp256"}:
        raise PlatformBundleError(f"{field} contains an unsupported SSH public key")
    try:
        base64.b64decode(fields[1], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise PlatformBundleError(f"{field} contains invalid SSH public-key data") from exc


def _validate_wireguard_public_key(value: Any, field: str, *, allow_pending: bool) -> None:
    if allow_pending and value == "":
        return
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9+/]{43}=", value):
        raise PlatformBundleError(f"{field} is invalid")
    try:
        if len(base64.b64decode(value, validate=True)) != 32:
            raise ValueError
    except (ValueError, binascii.Error) as exc:
        raise PlatformBundleError(f"{field} data is invalid") from exc


def validate_variables(variables: dict[str, Any]) -> None:
    if set(variables) != EXPECTED_VARIABLE_KEYS:
        raise PlatformBundleError("decrypted vars has an unexpected key set")
    operators = variables["platform_operator_ssh_public_keys"]
    if not isinstance(operators, list) or not operators:
        raise PlatformBundleError("platform_operator_ssh_public_keys must be a non-empty list")
    for key in operators:
        _validate_ssh_public_key(key, "platform_operator_ssh_public_keys")

    runner = variables["platform_runner"]
    runner_keys = {
        "architecture",
        "bootstrap_sha256",
        "bootstrap_version",
        "home",
        "install_dir",
        "labels",
        "name",
        "repository_url",
        "update_policy",
        "user",
        "work_dir",
    }
    if not isinstance(runner, dict) or set(runner) != runner_keys:
        raise PlatformBundleError("platform_runner has an invalid shape")
    if runner["architecture"] not in {"x64", "arm64"}:
        raise PlatformBundleError("platform_runner architecture is invalid")
    if not isinstance(runner["bootstrap_version"], str) or re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+", runner["bootstrap_version"]
    ) is None:
        raise PlatformBundleError("platform_runner bootstrap_version is invalid")
    if not isinstance(runner["bootstrap_sha256"], str) or re.fullmatch(
        r"[0-9a-f]{64}", runner["bootstrap_sha256"]
    ) is None:
        raise PlatformBundleError("platform_runner bootstrap_sha256 is invalid")
    if not isinstance(runner["repository_url"], str) or re.fullmatch(
        r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/?",
        runner["repository_url"],
    ) is None:
        raise PlatformBundleError("platform_runner repository_url is invalid")
    if not isinstance(runner["name"], str) or re.fullmatch(
        r"[A-Za-z0-9._-]{1,64}", runner["name"]
    ) is None:
        raise PlatformBundleError("platform_runner name is invalid")
    labels = runner["labels"]
    if (
        not isinstance(labels, list)
        or not labels
        or len(labels) != len(set(labels))
        or any(
            not isinstance(label, str)
            or re.fullmatch(r"[A-Za-z0-9._-]{1,64}", label) is None
            for label in labels
        )
    ):
        raise PlatformBundleError("platform_runner labels must be valid and unique")
    if runner["update_policy"] != "github-managed":
        raise PlatformBundleError("platform_runner update_policy is unsupported")
    fixed_layout = {
        "user": "github-runner",
        "home": "/var/lib/github-runner",
        "install_dir": "/opt/actions-runner",
        "work_dir": "_work",
    }
    if any(runner[key] != value for key, value in fixed_layout.items()):
        raise PlatformBundleError("platform_runner filesystem layout is unsupported")

    github_keys = variables["platform_github_ssh_keys"]
    if not isinstance(github_keys, list) or not github_keys:
        raise PlatformBundleError("platform_github_ssh_keys must be a non-empty list")
    environments: set[str] = set()
    for item in github_keys:
        if not isinstance(item, dict) or set(item) != {"environment", "public_key"}:
            raise PlatformBundleError("each platform_github_ssh_keys item has an invalid shape")
        environment = item["environment"]
        if environment not in {"develop", "prod"} or environment in environments:
            raise PlatformBundleError("GitHub key environment bindings must be valid and unique")
        environments.add(environment)
        _validate_ssh_public_key(item["public_key"], "platform_github_ssh_keys")

    _validate_cidrs(variables["platform_ssh_allowed_cidrs"], "platform_ssh_allowed_cidrs", require_one=True)
    _validate_cidrs(
        variables["platform_fail2ban_ignore_cidrs"],
        "platform_fail2ban_ignore_cidrs",
        require_one=False,
    )
    wireguard_interface = variables["platform_wireguard_interface"]
    if not isinstance(wireguard_interface, str) or re.fullmatch(
        r"[A-Za-z0-9_.-]{1,15}", wireguard_interface
    ) is None:
        raise PlatformBundleError("platform_wireguard_interface is invalid")

    wireguard_addresses = variables["platform_wireguard_hub_addresses"]
    if not isinstance(wireguard_addresses, dict) or set(wireguard_addresses) != {"develop", "prod"}:
        raise PlatformBundleError("platform_wireguard_hub_addresses must map develop and prod")
    hub_interfaces: dict[str, ipaddress.IPv4Interface] = {}
    for environment, address in wireguard_addresses.items():
        if not isinstance(address, str):
            raise PlatformBundleError("platform_wireguard_hub_addresses must contain strings")
        try:
            interface = ipaddress.ip_interface(address)
        except ValueError as exc:
            raise PlatformBundleError("platform_wireguard_hub_addresses contains an invalid address") from exc
        if interface.version != 4 or interface.network.prefixlen != 16:
            raise PlatformBundleError("management WireGuard addresses must use IPv4 /16 networks")
        hub_interfaces[environment] = interface

    environment_networks = variables["platform_wireguard_environment_networks"]
    if not isinstance(environment_networks, dict) or set(environment_networks) != {
        "develop",
        "prod",
    }:
        raise PlatformBundleError(
            "platform_wireguard_environment_networks must map develop and prod"
        )
    for environment, value in environment_networks.items():
        if not isinstance(value, str):
            raise PlatformBundleError(
                "platform_wireguard_environment_networks must contain strings"
            )
        try:
            network = ipaddress.ip_network(value, strict=True)
        except ValueError as exc:
            raise PlatformBundleError(
                "platform_wireguard_environment_networks contains an invalid network"
            ) from exc
        if network.version != 4 or network.prefixlen != 16:
            raise PlatformBundleError("management WireGuard networks must use IPv4 /16")
        if network != hub_interfaces[environment].network:
            raise PlatformBundleError(
                "management WireGuard networks must match their declared hub addresses"
            )

    listen_port = variables["platform_wireguard_listen_port"]
    if not isinstance(listen_port, int) or isinstance(listen_port, bool) or not 1 <= listen_port <= 65535:
        raise PlatformBundleError("platform_wireguard_listen_port is invalid")
    mtu = variables["platform_wireguard_mtu"]
    if not isinstance(mtu, int) or isinstance(mtu, bool) or not 1280 <= mtu <= 9000:
        raise PlatformBundleError("platform_wireguard_mtu is invalid")

    operator_peers = variables["platform_wireguard_operator_peers"]
    if not isinstance(operator_peers, list) or not operator_peers:
        raise PlatformBundleError("platform_wireguard_operator_peers must be a non-empty list")
    peer_ids: set[str] = set()
    peer_addresses: set[ipaddress.IPv4Address] = set()
    peer_public_keys: set[str] = set()
    for peer in operator_peers:
        if not isinstance(peer, dict) or set(peer) != {"id", "public_key", "allowed_ips"}:
            raise PlatformBundleError("each operator WireGuard peer has an invalid shape")
        peer_id = peer["id"]
        if not isinstance(peer_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", peer_id):
            raise PlatformBundleError("operator WireGuard peer ID is invalid")
        if peer_id in peer_ids:
            raise PlatformBundleError("operator WireGuard peer IDs must be unique")
        peer_ids.add(peer_id)
        public_key = peer["public_key"]
        _validate_wireguard_public_key(
            public_key,
            "operator WireGuard public key",
            allow_pending=False,
        )
        if public_key in peer_public_keys:
            raise PlatformBundleError("WireGuard public keys must be unique")
        peer_public_keys.add(public_key)
        _validate_cidrs(peer["allowed_ips"], "operator WireGuard allowed_ips", require_one=True)
        for value in peer["allowed_ips"]:
            network = ipaddress.ip_network(value)
            if network.version != 4 or network.prefixlen != 32:
                raise PlatformBundleError("operator WireGuard allowed_ips must contain only IPv4 /32 addresses")
            address = network.network_address
            if not any(address in hub.network for hub in hub_interfaces.values()):
                raise PlatformBundleError("operator WireGuard address is outside management networks")
            if address in {hub.ip for hub in hub_interfaces.values()} or address in peer_addresses:
                raise PlatformBundleError("operator WireGuard addresses must be unique and not use a hub address")
            peer_addresses.add(address)

    hub_public_key = variables["platform_wireguard_hub_public_key"]
    runner_peers = variables["platform_wireguard_runner_peers"]
    if not isinstance(runner_peers, list):
        raise PlatformBundleError("platform_wireguard_runner_peers must be a list")
    if runner_peers:
        _validate_wireguard_public_key(
            hub_public_key,
            "management hub WireGuard public key",
            allow_pending=False,
        )
    elif hub_public_key != "":
        _validate_wireguard_public_key(
            hub_public_key,
            "management hub WireGuard public key",
            allow_pending=False,
        )
    if hub_public_key:
        if hub_public_key in peer_public_keys:
            raise PlatformBundleError("management hub key must differ from every peer key")
        peer_public_keys.add(hub_public_key)
    for peer in runner_peers:
        if not isinstance(peer, dict) or set(peer) != {
            "address",
            "environment",
            "id",
            "interface",
            "persistent_keepalive_seconds",
            "public_key",
        }:
            raise PlatformBundleError("each runner WireGuard peer has an invalid shape")
        peer_id = peer["id"]
        if not isinstance(peer_id, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9-]{0,62}", peer_id
        ):
            raise PlatformBundleError("runner WireGuard peer ID is invalid")
        if peer_id in peer_ids:
            raise PlatformBundleError("WireGuard peer IDs must be unique")
        peer_ids.add(peer_id)
        environment = peer["environment"]
        if environment not in {"develop", "prod"}:
            raise PlatformBundleError("runner WireGuard environment is invalid")
        interface_name = peer["interface"]
        if not isinstance(interface_name, str) or re.fullmatch(
            r"[A-Za-z0-9_.-]{1,15}", interface_name
        ) is None:
            raise PlatformBundleError("runner WireGuard interface is invalid")
        try:
            runner = ipaddress.ip_interface(peer["address"])
        except (TypeError, ValueError) as exc:
            raise PlatformBundleError("runner WireGuard address is invalid") from exc
        if runner.version != 4 or runner.network.prefixlen != 32:
            raise PlatformBundleError("runner WireGuard address must be an IPv4 /32")
        environment_network = ipaddress.ip_network(environment_networks[environment])
        high_operator_range = ipaddress.ip_network(
            f"{environment_network.network_address + 65280}/24"
        )
        if (
            runner.ip not in high_operator_range
            or runner.ip in {hub.ip for hub in hub_interfaces.values()}
            or runner.ip in peer_addresses
        ):
            raise PlatformBundleError(
                "runner WireGuard address must be unique in the environment operator range"
            )
        peer_addresses.add(runner.ip)
        runner_public_key = peer["public_key"]
        _validate_wireguard_public_key(
            runner_public_key,
            "runner WireGuard public key",
            allow_pending=True,
        )
        if runner_public_key:
            if runner_public_key in peer_public_keys:
                raise PlatformBundleError("WireGuard public keys must be unique")
            peer_public_keys.add(runner_public_key)
        keepalive = peer["persistent_keepalive_seconds"]
        if (
            not isinstance(keepalive, int)
            or isinstance(keepalive, bool)
            or not 1 <= keepalive <= 65535
        ):
            raise PlatformBundleError("runner WireGuard keepalive is invalid")
    if not isinstance(variables["platform_vault_node_id"], str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9-]{0,62}", variables["platform_vault_node_id"]
    ):
        raise PlatformBundleError("platform_vault_node_id is invalid")
    if not isinstance(variables["platform_vault_tls_server_name"], str) or not re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?",
        variables["platform_vault_tls_server_name"],
    ):
        raise PlatformBundleError("platform_vault_tls_server_name is invalid")
    # Получатель сигнализации. Проверяется здесь, а не на хабе: Alertmanager
    # принимает почти любую строку как токен и молчит, а обнаруживается это
    # ровно тогда, когда кто-то ждёт уведомления о поломке.
    if not isinstance(variables["platform_alertmanager_telegram_bot_token"], str) or not re.fullmatch(
        r"[0-9]{5,}:[A-Za-z0-9_-]{30,}",
        variables["platform_alertmanager_telegram_bot_token"],
    ):
        raise PlatformBundleError("platform_alertmanager_telegram_bot_token is invalid")
    if not isinstance(variables["platform_alertmanager_telegram_chat_id"], str) or not re.fullmatch(
        r"-?[0-9]{1,32}", variables["platform_alertmanager_telegram_chat_id"]
    ):
        raise PlatformBundleError("platform_alertmanager_telegram_chat_id is invalid")
    # Тема супергруппы. Пустая строка — «без темы»: чат может и не быть форумом,
    # и тогда Telegram отвергнет сообщение с message_thread_id.
    if not isinstance(variables["platform_alertmanager_telegram_thread_id"], str) or not re.fullmatch(
        r"|[0-9]{1,32}", variables["platform_alertmanager_telegram_thread_id"]
    ):
        raise PlatformBundleError("platform_alertmanager_telegram_thread_id is invalid")


def decrypt_bundle(path: Path) -> tuple[dict[str, Any], str, dict[str, Any]]:
    sops = _require_command("sops")
    result = subprocess.run(
        [sops, "decrypt", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise PlatformBundleError(f"SOPS decryption failed: {message or 'unknown error'}")
    try:
        envelope = yaml.safe_load(result.stdout)
    except yaml.YAMLError as exc:
        raise PlatformBundleError("decrypted platform bundle is invalid YAML") from exc
    if not isinstance(envelope, dict) or set(envelope) != EXPECTED_KEYS:
        raise PlatformBundleError("decrypted platform bundle has an unexpected structure")
    if envelope["apiVersion"] != "spiritvpn.io/v1alpha1" or envelope["kind"] != "PlatformBootstrap":
        raise PlatformBundleError("decrypted platform bundle has an unsupported identity")
    for field in ("inventory", "known_hosts", "vars"):
        if not isinstance(envelope[field], str) or not envelope[field].strip():
            raise PlatformBundleError(f"decrypted {field} must be a non-empty string")
    variables = _yaml_mapping(envelope["vars"], "vars")
    validate_variables(variables)
    return (
        _yaml_mapping(envelope["inventory"], "inventory"),
        envelope["known_hosts"].strip() + "\n",
        variables,
    )


def _write_private(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _platform_public_host(inventory: dict[str, Any]) -> str:
    try:
        hosts = inventory["all"]["children"]["spiritvpn_platform_bootstrap"]["hosts"]
    except (KeyError, TypeError) as exc:
        raise PlatformBundleError("platform inventory has an unexpected structure") from exc
    if not isinstance(hosts, dict) or len(hosts) != 1:
        raise PlatformBundleError("platform inventory must contain exactly one host")
    hostvars = next(iter(hosts.values()))
    if not isinstance(hostvars, dict):
        raise PlatformBundleError("platform host variables are invalid")
    public_host = hostvars.get("ansible_host")
    if not isinstance(public_host, str) or not public_host:
        raise PlatformBundleError("platform inventory has no public management host")
    return public_host


def _wireguard_endpoint(public_host: str, listen_port: int) -> str:
    if not 1 <= listen_port <= 65535:
        raise PlatformBundleError("management WireGuard listen port is invalid")
    try:
        address = ipaddress.ip_address(public_host)
    except ValueError:
        if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", public_host) is None:
            raise PlatformBundleError("platform inventory public host is invalid")
        endpoint_host = public_host
    else:
        endpoint_host = f"[{address}]" if address.version == 6 else str(address)
    return f"{endpoint_host}:{listen_port}"


_MISSING = object()


def _report_access_drift(compare_applied_runtime: Path, desired: dict[str, Any]) -> None:
    """Сообщает, какие поля контракта доступа меняет этот прогон.

    Выкатку не прерывает: расхождение с применённым контрактом — сигнал для
    транскрипта, а не гейт. Смысл в том, чтобы смена ростера операторов не
    выглядела в логе так же, как смена MTU.

    Печатаются только имена полей. Значения не печатаются никогда: среди них
    лежит токен бота Alertmanager, а транскрипт исполнителя читают и хранят.
    """
    try:
        applied = yaml.safe_load(compare_applied_runtime.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        # Нечитаемого файла достаточно для молчания: это первый прогон на этом
        # хабе, а не расхождение. Отказ здесь останавливал бы выкатку ровно на
        # том хосте, у которого ещё нет ничего, что можно было бы сравнивать.
        return
    if not isinstance(applied, dict):
        return
    for key, default in TRANSITIONAL_RUNTIME_DEFAULTS.items():
        if key not in applied and desired[key] == default:
            applied[key] = copy.deepcopy(default)
    changed = sorted(key for key in desired if applied.get(key, _MISSING) != desired[key])
    # Ключ, который исчез из контракта, тоже изменение доступа: так пропадает
    # последний оператор из ростера, если поле снесли целиком.
    removed = sorted(set(applied) - set(desired))
    if not changed and not removed:
        return
    print(
        "контракт доступа изменяется этим прогоном; поля: "
        + ", ".join(changed + removed),
        file=sys.stderr,
    )


def materialize_runtime_variables(
    bundle: Path,
    output: Path,
    *,
    compare_applied_runtime: Path | None,
    executor_listen_port: int | None = None,
) -> None:
    inventory, _known_hosts, variables = decrypt_bundle(bundle)
    if (
        executor_listen_port is not None
        and executor_listen_port != variables["platform_wireguard_listen_port"]
    ):
        raise PlatformBundleError(
            "installed executor listen port differs from the Git-owned platform contract"
        )
    desired = {
        key: copy.deepcopy(variables[key])
        for key in RUNTIME_VARIABLE_KEYS
    }
    desired["platform_wireguard_public_endpoint"] = _wireguard_endpoint(
        _platform_public_host(inventory), variables["platform_wireguard_listen_port"]
    )

    if compare_applied_runtime is not None:
        _report_access_drift(compare_applied_runtime, desired)

    _write_private(
        output,
        yaml.safe_dump(desired, allow_unicode=True, sort_keys=True),
    )


def materialize_runner_plan(
    bundle: Path,
    output: Path,
    *,
    runner_id: str,
    source_git_sha: str,
) -> None:
    inventory, _known_hosts, variables = decrypt_bundle(bundle)
    matches = [
        peer
        for peer in variables["platform_wireguard_runner_peers"]
        if peer["id"] == runner_id
    ]
    if len(matches) != 1:
        raise PlatformBundleError("runner is not uniquely declared in the platform contract")
    peer = matches[0]
    environment = peer["environment"]
    hub_interface = ipaddress.ip_interface(
        variables["platform_wireguard_hub_addresses"][environment]
    )
    plan = {
        "schema_version": 1,
        "source_git_sha": source_git_sha,
        "artifacts": {
            "enrollment_script_sha256": hashlib.sha256(
                (REPOSITORY_ROOT / "scripts" / "enroll-runner-overlay.sh").read_bytes()
            ).hexdigest(),
        },
        "runner": {
            "address": peer["address"],
            "environment": environment,
            "id": peer["id"],
            "interface": peer["interface"],
            "public_key": peer["public_key"],
        },
        "hub": {
            "endpoint": _wireguard_endpoint(
                _platform_public_host(inventory),
                variables["platform_wireguard_listen_port"],
            ),
            "overlay_address": str(hub_interface.ip),
            "public_key": variables["platform_wireguard_hub_public_key"],
        },
        "environment_network": variables["platform_wireguard_environment_networks"][
            environment
        ],
        "persistent_keepalive_seconds": peer["persistent_keepalive_seconds"],
    }
    _write_private(
        output,
        yaml.safe_dump(plan, allow_unicode=True, sort_keys=True),
    )


def materialize_runner_host_plan(
    bundle: Path,
    output: Path,
    *,
    source_git_sha: str,
) -> None:
    _inventory, _known_hosts, variables = decrypt_bundle(bundle)
    plan = {
        "schema_version": 1,
        "source_git_sha": source_git_sha,
        "artifacts": {
            "bootstrap_script_sha256": hashlib.sha256(
                (REPOSITORY_ROOT / "scripts" / "bootstrap-self-hosted-runner.sh").read_bytes()
            ).hexdigest(),
        },
        "runner": copy.deepcopy(variables["platform_runner"]),
    }
    _write_private(
        output,
        json.dumps(plan, indent=2, sort_keys=True) + "\n",
    )


def _require_clean_exact_source(source_git_sha: str) -> None:
    if re.fullmatch(r"[0-9a-f]{40}", source_git_sha) is None:
        raise PlatformBundleError("runner plan requires a full Git commit SHA")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if (
        head.returncode != 0
        or status.returncode != 0
        or head.stdout.strip() != source_git_sha
        or status.stdout
    ):
        raise PlatformBundleError("runner plan requires the clean exact-SHA checkout")


def _run(command: list[str], *, environment: dict[str, str] | None = None) -> None:
    result = subprocess.run(command, cwd=REPOSITORY_ROOT, env=environment, check=False)
    if result.returncode != 0:
        raise PlatformBundleError(f"command failed with status {result.returncode}: {command[0]}")


def execute(mode: str, bundle: Path) -> None:
    inventory, known_hosts, variables = decrypt_bundle(bundle)
    with tempfile.TemporaryDirectory(prefix="spiritvpn-platform-") as temporary:
        protected = Path(temporary)
        os.chmod(protected, 0o700)
        inventory_path = protected / "inventory.yml"
        known_hosts_path = protected / "known_hosts"
        variables_path = protected / "vars.yml"
        component_variables_path = protected / "component-vars.yml"
        _write_private(
            inventory_path,
            yaml.safe_dump(inventory, allow_unicode=True, sort_keys=False),
        )
        _write_private(known_hosts_path, known_hosts)
        _write_private(
            variables_path,
            yaml.safe_dump(variables, allow_unicode=True, sort_keys=False),
        )
        _run(
            [
                sys.executable,
                str(REPOSITORY_ROOT / "scripts" / "platform-component-vars.py"),
                "--output",
                str(component_variables_path),
            ]
        )

        _run(
            [
                sys.executable,
                str(REPOSITORY_ROOT / "scripts" / "platform-bootstrap-check.py"),
                "--inventory",
                str(inventory_path),
                "--known-hosts",
                str(known_hosts_path),
            ]
        )

        if mode == "check":
            ansible_inventory = shutil.which("ansible-inventory")
            if ansible_inventory is None:
                print("ansible-inventory unavailable; platform parser check skipped", file=sys.stderr)
                return
            result = subprocess.run(
                [ansible_inventory, "-i", str(inventory_path), "--list"],
                cwd=REPOSITORY_ROOT,
                stdout=subprocess.DEVNULL,
                check=False,
            )
            if result.returncode != 0:
                raise PlatformBundleError("ansible-inventory rejected decrypted platform inventory")
            return

        ansible_playbook = _require_command("ansible-playbook")
        environment = os.environ.copy()
        environment["ANSIBLE_HOST_KEY_CHECKING"] = "True"
        environment["ANSIBLE_SSH_ARGS"] = f"-o UserKnownHostsFile={known_hosts_path}"
        command = [
            ansible_playbook,
            "-i",
            str(inventory_path),
            "playbooks/platform/bootstrap.yml",
            "--extra-vars",
            f"@{variables_path}",
            "--extra-vars",
            f"@{component_variables_path}",
        ]
        if mode == "bootstrap-check":
            command.append("--syntax-check")
            _run(command, environment=environment)
            ansible = _require_command("ansible")
            _run(
                [
                    ansible,
                    "-i",
                    str(inventory_path),
                    "spiritvpn_platform_bootstrap",
                    "--module-name",
                    "ping",
                    "--one-line",
                ],
                environment=environment,
            )
            return
        raise PlatformBundleError(f"unsupported non-mutating platform mode: {mode}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode",
        choices=(
            "check",
            "bootstrap-check",
            "materialize-runtime",
            "runner-host-plan",
            "runner-plan",
        ),
    )
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    # Accepted during the rollout from the executor installed before the listen
    # port became Git-owned. It is comparison-only and never drives projection.
    parser.add_argument("--wireguard-listen-port", type=int)
    # Сравнение, а не требование. Прежнее имя описывало отказ, которого больше
    # нет, и оставить его значило бы обещать гейт, которого не существует.
    parser.add_argument("--compare-applied-runtime", type=Path)
    # Прежнее имя того же аргумента, принимается молча.
    #
    # Без него выкатка этого изменения невозможна в принципе, и это не теория:
    # первый же прогон упал. Исполнитель на хабе — файл, отрендеренный прошлым
    # прогоном steady.yml, и он вызывает этот скрипт **до** того, как playbook
    # успеет заменить его новой версией. Переименовать флаг и шаблон одним
    # коммитом значит попросить установленного исполнителя передать аргумент,
    # о котором он ещё не знает, — и упасть раньше, чем он себя обновит.
    #
    # Снимать этот алиас можно только после того, как исполнитель перерендерен
    # во **всех** средах, включая prod: там автоматического пути нет, и хаб
    # может месяцами держать сборку, вызывающую старое имя.
    parser.add_argument(
        "--require-applied-runtime",
        dest="compare_applied_runtime",
        type=Path,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--runner-id")
    parser.add_argument("--source-git-sha")
    args = parser.parse_args()
    try:
        if args.mode == "materialize-runtime":
            if args.output is None:
                raise PlatformBundleError("materialize-runtime requires --output")
            if args.runner_id is not None or args.source_git_sha is not None:
                raise PlatformBundleError("runner options require runner-plan mode")
            materialize_runtime_variables(
                args.bundle.resolve(),
                args.output.resolve(),
                executor_listen_port=args.wireguard_listen_port,
                compare_applied_runtime=(
                    args.compare_applied_runtime.resolve()
                    if args.compare_applied_runtime is not None
                    else None
                ),
            )
        elif args.mode == "runner-plan":
            if args.output is None or args.runner_id is None or args.source_git_sha is None:
                raise PlatformBundleError(
                    "runner-plan requires --output, --runner-id and --source-git-sha"
                )
            if args.wireguard_listen_port is not None or args.compare_applied_runtime is not None:
                raise PlatformBundleError("runtime options cannot be used with runner-plan")
            _require_clean_exact_source(args.source_git_sha)
            materialize_runner_plan(
                args.bundle.resolve(),
                args.output.resolve(),
                runner_id=args.runner_id,
                source_git_sha=args.source_git_sha,
            )
        elif args.mode == "runner-host-plan":
            if args.output is None or args.source_git_sha is None:
                raise PlatformBundleError(
                    "runner-host-plan requires --output and --source-git-sha"
                )
            if (
                args.runner_id is not None
                or args.wireguard_listen_port is not None
                or args.compare_applied_runtime is not None
            ):
                raise PlatformBundleError(
                    "overlay or runtime options cannot be used with runner-host-plan"
                )
            _require_clean_exact_source(args.source_git_sha)
            materialize_runner_host_plan(
                args.bundle.resolve(),
                args.output.resolve(),
                source_git_sha=args.source_git_sha,
            )
        else:
            if any(
                value is not None
                for value in (
                    args.output,
                    args.wireguard_listen_port,
                    args.compare_applied_runtime,
                    args.runner_id,
                    args.source_git_sha,
                )
            ):
                raise PlatformBundleError(
                    "runtime materialization options require materialize-runtime mode"
                )
            execute(args.mode, args.bundle.resolve())
    except (OSError, PlatformBundleError) as exc:
        print(f"platform bundle failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
