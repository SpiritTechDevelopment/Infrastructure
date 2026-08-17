#!/usr/bin/env python3
"""Decrypt and use the SOPS-sealed one-host platform bootstrap bundle."""

from __future__ import annotations

import argparse
import base64
import binascii
import ipaddress
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
    "platform_fail2ban_ignore_cidrs",
    "platform_github_ssh_keys",
    "platform_operator_ssh_public_keys",
    "platform_ssh_allowed_cidrs",
    "platform_vault_node_id",
    "platform_vault_tls_server_name",
    "platform_wireguard_hub_addresses",
    "platform_wireguard_operator_peers",
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


def validate_variables(variables: dict[str, Any]) -> None:
    if set(variables) != EXPECTED_VARIABLE_KEYS:
        raise PlatformBundleError("decrypted vars has an unexpected key set")
    operators = variables["platform_operator_ssh_public_keys"]
    if not isinstance(operators, list) or not operators:
        raise PlatformBundleError("platform_operator_ssh_public_keys must be a non-empty list")
    for key in operators:
        _validate_ssh_public_key(key, "platform_operator_ssh_public_keys")

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
    wireguard_addresses = variables["platform_wireguard_hub_addresses"]
    if not isinstance(wireguard_addresses, dict) or set(wireguard_addresses) != {"develop", "prod"}:
        raise PlatformBundleError("platform_wireguard_hub_addresses must map develop and prod")
    hub_interfaces: list[ipaddress.IPv4Interface] = []
    for address in wireguard_addresses.values():
        if not isinstance(address, str):
            raise PlatformBundleError("platform_wireguard_hub_addresses must contain strings")
        try:
            interface = ipaddress.ip_interface(address)
        except ValueError as exc:
            raise PlatformBundleError("platform_wireguard_hub_addresses contains an invalid address") from exc
        if interface.version != 4 or interface.network.prefixlen != 16:
            raise PlatformBundleError("management WireGuard addresses must use IPv4 /16 networks")
        hub_interfaces.append(interface)

    operator_peers = variables["platform_wireguard_operator_peers"]
    if not isinstance(operator_peers, list) or not operator_peers:
        raise PlatformBundleError("platform_wireguard_operator_peers must be a non-empty list")
    peer_ids: set[str] = set()
    peer_addresses: set[ipaddress.IPv4Address] = set()
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
        if not isinstance(public_key, str) or not re.fullmatch(r"[A-Za-z0-9+/]{43}=", public_key):
            raise PlatformBundleError("operator WireGuard public key is invalid")
        try:
            if len(base64.b64decode(public_key, validate=True)) != 32:
                raise ValueError
        except (ValueError, binascii.Error) as exc:
            raise PlatformBundleError("operator WireGuard public key data is invalid") from exc
        _validate_cidrs(peer["allowed_ips"], "operator WireGuard allowed_ips", require_one=True)
        for value in peer["allowed_ips"]:
            network = ipaddress.ip_network(value)
            if network.version != 4 or network.prefixlen != 32:
                raise PlatformBundleError("operator WireGuard allowed_ips must contain only IPv4 /32 addresses")
            address = network.network_address
            if not any(address in hub.network for hub in hub_interfaces):
                raise PlatformBundleError("operator WireGuard address is outside management networks")
            if address in {hub.ip for hub in hub_interfaces} or address in peer_addresses:
                raise PlatformBundleError("operator WireGuard addresses must be unique and not use a hub address")
            peer_addresses.add(address)
    if not isinstance(variables["platform_vault_node_id"], str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9-]{0,62}", variables["platform_vault_node_id"]
    ):
        raise PlatformBundleError("platform_vault_node_id is invalid")
    if not isinstance(variables["platform_vault_tls_server_name"], str) or not re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?",
        variables["platform_vault_tls_server_name"],
    ):
        raise PlatformBundleError("platform_vault_tls_server_name is invalid")


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
    parser.add_argument("mode", choices=("check", "bootstrap-check"))
    parser.add_argument("--bundle", required=True, type=Path)
    args = parser.parse_args()
    try:
        execute(args.mode, args.bundle.resolve())
    except (OSError, PlatformBundleError) as exc:
        print(f"platform bundle failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
