#!/usr/bin/env python3
"""Two-phase clean-host management bootstrap with automatic operator WireGuard.

With --reuse-tunnel the first phase is skipped and only the second one runs, over the
management tunnel that an earlier bootstrap built. That is the supported way to deliver
reviewed bundle values to a hub whose public SSH is already closed.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import copy
import importlib.util
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CLIENT_MARKER = "# Managed by SpiritVPN platform bootstrap"


class BootstrapError(Exception):
    """Raised when the two-phase bootstrap cannot continue safely."""


def _load_platform_sops() -> ModuleType:
    path = REPOSITORY_ROOT / "scripts" / "platform-sops.py"
    spec = importlib.util.spec_from_file_location("spiritvpn_platform_sops", path)
    if spec is None or spec.loader is None:
        raise BootstrapError("cannot load the platform SOPS helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _require_command(command: str) -> str:
    resolved = shutil.which(command)
    if resolved is None:
        raise BootstrapError(f"required command is unavailable: {command}")
    return resolved


def _run(
    command: list[str],
    *,
    environment: dict[str, str] | None = None,
    input_data: bytes | None = None,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        env=environment,
        input=input_data,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=False,
    )
    if check and result.returncode != 0:
        detail = ""
        if capture:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise BootstrapError(
            f"command failed with status {result.returncode}: {command[0]}"
            + (f": {detail}" if detail else "")
        )
    return result


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


def _materialize_platform_component_vars(path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(REPOSITORY_ROOT / "scripts" / "platform-component-vars.py"),
            "--output",
            str(path),
        ],
        cwd=REPOSITORY_ROOT,
        check=False,
    )
    if result.returncode != 0:
        raise BootstrapError("cannot materialize platform images from common desired state")


def _one_platform_host(inventory: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    try:
        hosts = inventory["all"]["children"]["spiritvpn_platform_bootstrap"]["hosts"]
    except (KeyError, TypeError) as exc:
        raise BootstrapError("platform inventory has an unexpected structure") from exc
    if not isinstance(hosts, dict) or len(hosts) != 1:
        raise BootstrapError("platform inventory must contain exactly one host")
    name, values = next(iter(hosts.items()))
    if not isinstance(values, dict):
        raise BootstrapError("platform host variables are invalid")
    return name, values


def _validate_private_key(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise BootstrapError(f"operator WireGuard private key is missing or unsafe: {path}")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise BootstrapError(f"operator WireGuard private key must have mode 0600: {path}")
    wg = _require_command("wg")
    result = _run([wg, "pubkey"], input_data=path.read_bytes(), capture=True)
    public_key = result.stdout.decode("ascii", errors="strict").strip()
    try:
        decoded = base64.b64decode(public_key, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise BootstrapError("wg returned an invalid operator public key") from exc
    if len(decoded) != 32:
        raise BootstrapError("wg returned an invalid operator public key length")
    return public_key


def _select_operator_peer(variables: dict[str, Any], public_key: str) -> dict[str, Any]:
    peers = [
        peer
        for peer in variables["platform_wireguard_operator_peers"]
        if peer["public_key"] == public_key
    ]
    if len(peers) != 1:
        raise BootstrapError(
            "the local WireGuard private key must match exactly one encrypted operator peer"
        )
    return peers[0]


def _load_metadata(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BootstrapError("management WireGuard metadata is missing or invalid") from exc
    if not isinstance(value, dict) or set(value) != {
        "hub_addresses",
        "interface",
        "listen_port",
        "public_key",
    }:
        raise BootstrapError("management WireGuard metadata has an unexpected structure")
    if not isinstance(value["interface"], str) or re.fullmatch(
        r"[A-Za-z0-9_.-]{1,15}", value["interface"]
    ) is None:
        raise BootstrapError("management WireGuard interface is invalid")
    if not isinstance(value["listen_port"], int) or not 1 <= value["listen_port"] <= 65535:
        raise BootstrapError("management WireGuard listen port is invalid")
    if not isinstance(value["hub_addresses"], dict):
        raise BootstrapError("management WireGuard hub addresses are invalid")
    try:
        decoded = base64.b64decode(value["public_key"], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise BootstrapError("management WireGuard public key is invalid") from exc
    if len(decoded) != 32:
        raise BootstrapError("management WireGuard public key length is invalid")
    return value


def _client_values(
    peer: dict[str, Any], hub_addresses: dict[str, Any]
) -> tuple[list[str], list[str], str]:
    hub_interfaces = {
        environment: ipaddress.ip_interface(address)
        for environment, address in hub_addresses.items()
    }
    client_addresses: list[str] = []
    allowed_networks: set[str] = set()
    preferred_internal_host: str | None = None
    for value in peer["allowed_ips"]:
        interface = ipaddress.ip_interface(value)
        matching = [
            (environment, hub)
            for environment, hub in hub_interfaces.items()
            if interface.ip in hub.network
        ]
        if len(matching) != 1:
            raise BootstrapError("operator address does not match exactly one management network")
        environment, hub = matching[0]
        client_addresses.append(str(interface))
        allowed_networks.add(str(hub.network))
        if preferred_internal_host is None or environment == "develop":
            preferred_internal_host = str(hub.ip)
    if preferred_internal_host is None:
        raise BootstrapError("operator peer has no usable management address")
    return sorted(client_addresses), sorted(allowed_networks), preferred_internal_host


def _sudo_prefix() -> list[str]:
    return [] if os.geteuid() == 0 else [_require_command("sudo")]


def _install_client_config(interface: str, content: str) -> None:
    if re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", interface) is None:
        raise BootstrapError("local WireGuard interface name is invalid")
    target = Path("/etc/wireguard") / f"{interface}.conf"
    sudo = _sudo_prefix()
    with tempfile.TemporaryDirectory(prefix="spiritvpn-wireguard-client-") as temporary:
        protected = Path(temporary)
        os.chmod(protected, 0o700)
        candidate = protected / "client.conf"
        _write_private(candidate, content)

        exists = _run([*sudo, "test", "-e", str(target)], check=False).returncode == 0
        if exists:
            first_line = _run(
                [*sudo, "head", "-n", "1", str(target)], capture=True
            ).stdout.decode("utf-8", errors="replace").strip()
            if first_line != CLIENT_MARKER:
                raise BootstrapError(f"refusing to overwrite unmanaged WireGuard config: {target}")
        same = exists and _run(
            [*sudo, "cmp", "--silent", str(candidate), str(target)], check=False
        ).returncode == 0
        active = _run(
            ["ip", "link", "show", "dev", interface], check=False, capture=True
        ).returncode == 0
        if same and active:
            print(f"operator WireGuard {interface}: unchanged and active")
            return
        if active:
            _run([*sudo, "wg-quick", "down", interface])
        _run([*sudo, "install", "-o", "root", "-g", "root", "-m", "0600", str(candidate), str(target)])
        _run([*sudo, "wg-quick", "up", interface])
        if shutil.which("systemctl") is not None:
            _run([*sudo, "systemctl", "enable", f"wg-quick@{interface}.service"])


def _reused_tunnel_endpoint(interface: str, public_host: str, expected_port: int) -> str:
    """Recover the hub's public endpoint from the tunnel a previous bootstrap made.

    Фаза 1 недостижима на захардененном хабе: публичный SSH закрыт по замыслу, и
    прогон висит на ping-проверке. Но всё, чего не хватает второй фазе, уже лежит
    в локальном конфиге, который её же первая фаза и записала. Читаем оттуда, а не
    угадываем: порт хаба — его собственное состояние, а не константа операторской
    станции.
    """
    if re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", interface) is None:
        raise BootstrapError("local WireGuard interface name is invalid")
    target = Path("/etc/wireguard") / f"{interface}.conf"
    sudo = _sudo_prefix()
    if _run([*sudo, "test", "-e", str(target)], check=False).returncode != 0:
        raise BootstrapError(
            f"no management tunnel to reuse: {target} is missing; bootstrap the hub first"
        )
    lines = _run(
        [*sudo, "cat", str(target)], capture=True
    ).stdout.decode("utf-8", errors="replace").splitlines()
    if not lines or lines[0].strip() != CLIENT_MARKER:
        raise BootstrapError(f"refusing to reuse an unmanaged WireGuard config: {target}")
    endpoints = [
        match.group(1)
        for match in (re.fullmatch(r"\s*Endpoint\s*=\s*(\S+)\s*", line) for line in lines)
        if match is not None
    ]
    if len(endpoints) != 1:
        raise BootstrapError(f"managed WireGuard config declares no single endpoint: {target}")
    endpoint = endpoints[0]
    if re.fullmatch(r"[A-Za-z0-9.:\[\]-]+:[0-9]{1,5}", endpoint) is None:
        raise BootstrapError("managed WireGuard endpoint is invalid")
    if endpoint.rsplit(":", 1)[0] != public_host:
        raise BootstrapError(
            "the management tunnel does not lead to the hub this bundle describes"
        )
    if int(endpoint.rsplit(":", 1)[1]) != expected_port:
        raise BootstrapError(
            "the management tunnel endpoint does not use the Git-owned listen port"
        )
    if _run(["ip", "link", "show", "dev", interface], check=False, capture=True).returncode != 0:
        raise BootstrapError(f"management tunnel {interface} is not up")
    return endpoint


def _internal_known_hosts(known_hosts: str, public_host: str, internal_host: str) -> str:
    lines = [line for line in known_hosts.splitlines() if line.strip()]
    added: list[str] = []
    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            continue
        names = fields[0].split(",")
        if public_host in names or f"[{public_host}]:22" in names:
            added.append(f"{internal_host} {fields[1]} {fields[2]}")
    if not added:
        raise BootstrapError("cannot bind the pinned public SSH host key to the tunnel address")
    return "\n".join([*lines, *added]) + "\n"


def _ansible_environment(known_hosts_path: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["ANSIBLE_HOST_KEY_CHECKING"] = "True"
    environment["ANSIBLE_SSH_ARGS"] = f"-o UserKnownHostsFile={known_hosts_path}"
    return environment


def execute(
    *,
    bundle: Path,
    operator_private_key: Path,
    client_interface: str,
    verify_convergence: bool,
    reuse_tunnel: bool,
) -> None:
    platform_sops = _load_platform_sops()
    inventory, known_hosts, variables = platform_sops.decrypt_bundle(bundle)
    _, public_hostvars = _one_platform_host(inventory)
    public_host = str(public_hostvars["ansible_host"])
    operator_public_key = _validate_private_key(operator_private_key)
    peer = _select_operator_peer(variables, operator_public_key)
    ansible_playbook = _require_command("ansible-playbook")
    ansible = _require_command("ansible")

    with tempfile.TemporaryDirectory(prefix="spiritvpn-platform-bootstrap-") as temporary:
        protected = Path(temporary)
        os.chmod(protected, 0o700)
        public_inventory_path = protected / "public-inventory.yml"
        public_known_hosts_path = protected / "public-known-hosts"
        variables_path = protected / "vars.yml"
        component_variables_path = protected / "component-vars.yml"
        metadata_path = protected / "wireguard-metadata.json"
        _write_private(public_inventory_path, yaml.safe_dump(inventory, sort_keys=False))
        _write_private(public_known_hosts_path, known_hosts)
        _write_private(variables_path, yaml.safe_dump(variables, sort_keys=False))
        _materialize_platform_component_vars(component_variables_path)

        _run(
            [
                sys.executable,
                str(REPOSITORY_ROOT / "scripts" / "platform-bootstrap-check.py"),
                "--inventory",
                str(public_inventory_path),
                "--known-hosts",
                str(public_known_hosts_path),
            ]
        )
        public_environment = _ansible_environment(public_known_hosts_path)
        _run(
            [
                ansible_playbook,
                "-i",
                str(public_inventory_path),
                "playbooks/platform/bootstrap.yml",
                "--extra-vars",
                f"@{variables_path}",
                "--extra-vars",
                f"@{component_variables_path}",
                "--extra-vars",
                json.dumps(
                    {
                        "platform_wireguard_public_endpoint": (
                            f"{public_host}:{variables['platform_wireguard_listen_port']}"
                        )
                    }
                ),
                "--syntax-check",
            ],
            environment=public_environment,
        )
        if reuse_tunnel:
            public_endpoint = _reused_tunnel_endpoint(
                client_interface,
                public_host,
                variables["platform_wireguard_listen_port"],
            )
            _, _, internal_host = _client_values(
                peer, variables["platform_wireguard_hub_addresses"]
            )
            print(
                "reusing the existing management tunnel; the public phase is closed on a "
                "hardened hub by design"
            )
        else:
            _run(
                [
                    ansible,
                    "-i",
                    str(public_inventory_path),
                    "spiritvpn_platform_bootstrap",
                    "--module-name",
                    "ping",
                    "--one-line",
                ],
                environment=public_environment,
            )

            print("phase 1/2: creating management WireGuard before firewall hardening")
            _run(
                [
                    ansible_playbook,
                    "-i",
                    str(public_inventory_path),
                    "playbooks/platform/wireguard-bootstrap.yml",
                    "--extra-vars",
                    f"@{variables_path}",
                    "--extra-vars",
                    json.dumps({"platform_wireguard_metadata_output": str(metadata_path)}),
                ],
                environment=public_environment,
            )
            metadata = _load_metadata(metadata_path)
            client_addresses, allowed_networks, internal_host = _client_values(
                peer, metadata["hub_addresses"]
            )
            public_endpoint = f"{public_host}:{metadata['listen_port']}"
            private_key = operator_private_key.read_text(encoding="ascii").strip()
            client_config = (
                f"{CLIENT_MARKER}\n"
                "[Interface]\n"
                f"PrivateKey = {private_key}\n"
                f"Address = {', '.join(client_addresses)}\n\n"
                "[Peer]\n"
                f"PublicKey = {metadata['public_key']}\n"
                f"AllowedIPs = {', '.join(allowed_networks)}\n"
                f"Endpoint = {public_endpoint}\n"
                "PersistentKeepalive = 25\n"
            )
            _install_client_config(client_interface, client_config)
            private_key = ""
            client_config = ""

        internal_inventory = copy.deepcopy(inventory)
        _, internal_hostvars = _one_platform_host(internal_inventory)
        internal_hostvars["ansible_host"] = internal_host
        internal_known_hosts = _internal_known_hosts(known_hosts, public_host, internal_host)
        internal_inventory_path = protected / "internal-inventory.yml"
        internal_known_hosts_path = protected / "internal-known-hosts"
        runtime_variables_path = protected / "runtime-vars.yml"
        runtime_variables = {
            key: copy.deepcopy(variables[key])
            for key in platform_sops.RUNTIME_VARIABLE_KEYS
        }
        runtime_variables["platform_wireguard_public_endpoint"] = public_endpoint
        _write_private(
            internal_inventory_path,
            yaml.safe_dump(internal_inventory, sort_keys=False),
        )
        _write_private(internal_known_hosts_path, internal_known_hosts)
        _write_private(runtime_variables_path, yaml.safe_dump(runtime_variables, sort_keys=False))
        internal_environment = _ansible_environment(internal_known_hosts_path)

        print(f"verifying pinned SSH through WireGuard at {internal_host}")
        _run(
            [
                ansible,
                "-i",
                str(internal_inventory_path),
                "spiritvpn_platform_bootstrap",
                "--module-name",
                "ping",
                "--one-line",
            ],
            environment=internal_environment,
        )

        print("phase 2/2: applying hardening, Docker, Vault and restricted executors")
        apply_command = [
            ansible_playbook,
            "-i",
            str(internal_inventory_path),
            "playbooks/platform/bootstrap.yml",
            "--extra-vars",
            f"@{runtime_variables_path}",
            "--extra-vars",
            f"@{component_variables_path}",
        ]
        if reuse_tunnel:
            # Хаб уже захарденен, и говорить ему обратное — регресс: режим bootstrap
            # держит открытым ещё и порт 22, чтобы не оборвать себе связь посреди
            # первого прогона. Здесь обрывать нечего, связь идёт по оверлею.
            apply_command += ["--extra-vars", json.dumps({"deploy_mode": "hardened"})]
        _run(apply_command, environment=internal_environment)
        if verify_convergence:
            print("verifying convergence with a second idempotent apply")
            _run(apply_command, environment=internal_environment)
        if reuse_tunnel:
            print(
                "platform bundle applied over the tunnel; the hub persisted it as its "
                "runtime configuration, and platform-deploy now reconciles the steady state "
                "from those values"
            )
        else:
            print(
                f"platform bootstrap complete; operator peer {peer['id']} uses {client_interface}"
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--operator-wireguard-private-key", required=True, type=Path)
    parser.add_argument("--client-interface", default="spiritvpn-mgmt")
    parser.add_argument("--verify-convergence", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--reuse-tunnel",
        action="store_true",
        help=(
            "apply the bundle to an already-hardened hub through the management tunnel a "
            "previous bootstrap created, instead of building one over public SSH"
        ),
    )
    args = parser.parse_args()
    if not args.apply:
        print("platform bootstrap failed: refusing mutation without --apply", file=sys.stderr)
        return 2
    try:
        execute(
            bundle=args.bundle.resolve(),
            operator_private_key=args.operator_wireguard_private_key.resolve(),
            client_interface=args.client_interface,
            verify_convergence=args.verify_convergence,
            reuse_tunnel=args.reuse_tunnel,
        )
    except (BootstrapError, OSError) as exc:
        print(f"platform bootstrap failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
