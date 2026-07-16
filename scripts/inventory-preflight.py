#!/usr/bin/env python3
"""Fail-fast validation for the complete public-runtime/no-hardening inventory."""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


class Problems:
    def __init__(self) -> None:
        self.items: list[str] = []

    def require(self, condition: bool, message: str) -> None:
        if not condition:
            self.items.append(message)

    def finish(self) -> None:
        if self.items:
            print("Inventory preflight failed:", file=sys.stderr)
            for item in self.items:
                print(f"  - {item}", file=sys.stderr)
            raise SystemExit(1)


def run(*argv: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def inventory_data(path: Path) -> dict[str, Any]:
    proc = run("ansible-inventory", "-i", str(path), "--list")
    if proc.returncode:
        raise SystemExit(f"ansible-inventory failed:\n{proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid ansible-inventory JSON: {exc}") from exc


def group_hosts(data: dict[str, Any], group: str) -> list[str]:
    group_data = data.get(group, {})
    hosts = group_data.get("hosts", []) if isinstance(group_data, dict) else []
    return list(hosts or [])


def enabled(hostvars: dict[str, Any], host: str) -> bool:
    return bool(hostvars.get(host, {}).get("node_enabled", True))


def is_placeholder(value: Any) -> bool:
    text = str(value or "")
    return not text or "REPLACE_" in text or "REPLACE WITH" in text.upper()


def executable(name: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory, name)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def parse_port(value: Any, label: str, problems: Problems) -> int | None:
    try:
        port = int(value)
    except (TypeError, ValueError):
        problems.require(False, f"{label}: port is not an integer: {value!r}")
        return None
    problems.require(1 <= port <= 65535, f"{label}: port is outside 1..65535: {port}")
    return port


def validate_pem_pair(
    cert: str,
    key: str,
    label: str,
    problems: Problems,
    hostnames: Iterable[str] = (),
) -> None:
    if is_placeholder(cert) or is_placeholder(key):
        problems.require(False, f"{label}: certificate/private key is empty or a placeholder")
        return
    if not executable("openssl"):
        problems.require(False, f"{label}: openssl is required for certificate validation")
        return
    with tempfile.TemporaryDirectory(prefix="infra-preflight-") as tmp:
        cert_path = Path(tmp, "cert.pem")
        key_path = Path(tmp, "key.pem")
        cert_path.write_text(cert, encoding="utf-8")
        key_path.write_text(key, encoding="utf-8")
        cert_check = run("openssl", "x509", "-in", str(cert_path), "-noout")
        key_check = run("openssl", "pkey", "-in", str(key_path), "-noout", "-check")
        problems.require(cert_check.returncode == 0, f"{label}: invalid certificate PEM")
        problems.require(key_check.returncode == 0, f"{label}: invalid unencrypted private key PEM")
        if cert_check.returncode != 0 or key_check.returncode != 0:
            return

        cert_pub = run("openssl", "x509", "-in", str(cert_path), "-pubkey", "-noout")
        key_pub = run("openssl", "pkey", "-in", str(key_path), "-pubout")
        problems.require(
            cert_pub.returncode == 0
            and key_pub.returncode == 0
            and cert_pub.stdout.strip() == key_pub.stdout.strip(),
            f"{label}: certificate and private key do not match",
        )
        for hostname in hostnames:
            if not hostname:
                continue
            check = run("openssl", "x509", "-in", str(cert_path), "-noout", "-checkhost", hostname)
            problems.require(check.returncode == 0, f"{label}: certificate does not cover {hostname}")



def materialize_file_lookup(value: Any, label: str, problems: Problems) -> Any:
    """Resolve the repository's explicit playbook_dir-based file lookups.

    ansible-inventory intentionally returns lazy Jinja strings instead of evaluating
    lookup('file', ...). The deploy playbook evaluates them later, but preflight must
    validate the real certificate/password bytes before touching any host.
    """
    if not isinstance(value, str) or "lookup(" not in value or "playbook_dir" not in value:
        return value
    match = re.search(r"playbook_dir\s*~\s*['\"]([^'\"]+)['\"]", value)
    if not match:
        problems.require(False, f"{label}: unsupported file-lookup expression")
        return ""
    relative = match.group(1).lstrip("/")
    playbook_dir = Path(__file__).resolve().parent.parent / "playbooks"
    candidate = (playbook_dir / relative).resolve()
    repo_root = Path(__file__).resolve().parent.parent
    try:
        candidate.relative_to(repo_root)
    except ValueError:
        problems.require(False, f"{label}: file lookup escapes the repository: {candidate}")
        return ""
    if not candidate.is_file():
        problems.require(False, f"{label}: referenced file does not exist: {candidate}")
        return ""
    try:
        content = candidate.read_text(encoding="utf-8")
    except OSError as exc:
        problems.require(False, f"{label}: cannot read {candidate}: {exc}")
        return ""
    return content.strip() if re.search(r"\|\s*trim\b", value) else content


def literal_ip(value: str) -> str | None:
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None

def resolve_hostname(name: str) -> set[str]:
    try:
        return {item[4][0] for item in socket.getaddrinfo(name, None)}
    except socket.gaierror:
        return set()


def address_identities(value: str) -> set[str]:
    """Return the IP identities represented by an IP literal or DNS name."""
    try:
        return {str(ipaddress.ip_address(value))}
    except ValueError:
        return resolve_hostname(value)


def require_unique_ports(ports: dict[str, int | None], label: str, problems: Problems) -> None:
    seen: dict[int, str] = {}
    for name, port in ports.items():
        if port is None:
            continue
        if port in seen:
            problems.require(False, f"{label}: {name} port {port} collides with {seen[port]}")
        else:
            seen[port] = name


def validate_image_reference(value: Any, label: str, problems: Problems) -> None:
    image = str(value or "").strip()
    problems.require(not is_placeholder(image), f"{label}: image reference is missing/placeholder")
    if not image or is_placeholder(image):
        return
    final = image.rsplit("/", 1)[-1]
    pinned = "@sha256:" in image or (":" in final and not final.endswith(":latest"))
    problems.require(pinned, f"{label}: image must use an explicit non-latest tag or digest")


def validate_http_endpoint(
    value: Any,
    expected_host: str,
    expected_port: int,
    expected_path: str,
    label: str,
    problems: Problems,
) -> None:
    raw = re.sub(r"{{\s*control_plane_host\s*}}", expected_host, str(value or ""))
    try:
        parsed = urlparse(raw)
        port = parsed.port
    except ValueError:
        parsed = urlparse("")
        port = None
    problems.require(parsed.scheme == "http", f"{label}: must use http in current public runtime")
    problems.require(parsed.hostname == expected_host, f"{label}: must point to control_plane_host {expected_host}")
    problems.require(port == expected_port, f"{label}: must use port {expected_port}")
    problems.require(parsed.path == expected_path, f"{label}: must use path {expected_path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--skip-dns", action="store_true")
    args = parser.parse_args()

    data = inventory_data(args.inventory)
    hostvars: dict[str, dict[str, Any]] = data.get("_meta", {}).get("hostvars", {})
    problems = Problems()

    platform = [h for h in group_hosts(data, "platform") if enabled(hostvars, h)]
    entries = [h for h in group_hosts(data, "entry") if enabled(hostvars, h)]
    exits = [h for h in group_hosts(data, "exit") if enabled(hostvars, h)]
    fleet = entries + exits

    problems.require(len(platform) == 1, "exactly one enabled platform host is required")
    problems.require(bool(entries), "at least one enabled entry is required")
    problems.require(bool(exits), "at least one enabled exit is required")

    hardening_flags = (
        "common_manage_deploy_user",
        "common_remove_legacy_sshd_dropin",
        "common_manage_sshd",
        "common_manage_firewall",
        "common_enable_fail2ban",
        "common_manage_sysctl",
        "common_enable_auditd",
        "common_enable_unattended_upgrades",
        "management_wireguard_enabled",
        "docker_manage_daemon_config",
        "docker_remove_legacy_no_new_privileges",
    )
    for host in platform + fleet:
        values = hostvars.get(host, {})
        for flag in hardening_flags:
            problems.require(not bool(values.get(flag, False)), f"{host}: {flag} must remain false")

    active_tags: set[str] = set()
    service_uuids: set[str] = set()
    public_names: set[str] = set()
    public_addresses: set[str] = set()
    api_endpoints: set[tuple[str, int]] = set()

    for host in fleet:
        values = hostvars.get(host, {})
        role = values.get("node_role")
        problems.require(role in {"entry", "exit"}, f"{host}: node_role must be entry or exit")
        problems.require(not is_placeholder(values.get("ansible_user")), f"{host}: ansible_user is missing/placeholder")
        validate_image_reference(values.get("xray_image", "ghcr.io/xtls/xray-core:26.3.27"), f"{host} Xray", problems)
        validate_image_reference(values.get("nginx_mask_image", "nginx:1.29-alpine"), f"{host} nginx mask", problems)
        validate_image_reference(values.get("alloy_image", "grafana/alloy:v1.17.1"), f"{host} Alloy", problems)
        validate_image_reference(values.get("node_exporter_image", "quay.io/prometheus/node-exporter:v1.9.1"), f"{host} node_exporter", problems)
        problems.require(bool(values.get("xray_enable_api")), f"{host}: xray_enable_api must be true")
        problems.require(bool(values.get("xray_api_public_mode")), f"{host}: xray_api_public_mode must be true")
        problems.require(
            values.get("xray_api_bind") in {"0.0.0.0", "::"},
            f"{host}: API must bind publicly in current mode",
        )
        services = set(values.get("xray_api_services", []))
        problems.require(
            {"HandlerService", "StatsService"}.issubset(services),
            f"{host}: HandlerService and StatsService are required",
        )
        problems.require(bool(values.get("xray_stats_user_traffic", True)), f"{host}: per-user traffic stats must be enabled")
        problems.require(
            str(values.get("xray_access_log", "")).lower() != "none",
            f"{host}: Xray access logging must not be disabled",
        )
        problems.require(
            str(values.get("xray_error_log", "")).lower() != "none",
            f"{host}: Xray error logging must not be disabled",
        )

        ansible_host = str(values.get("ansible_host", ""))
        public_name = str(values.get("public_hostname") or ansible_host)
        api_host = str(values.get("xray_api_public_host") or ansible_host)
        problems.require(not is_placeholder(ansible_host), f"{host}: ansible_host is missing/placeholder")
        problems.require(not is_placeholder(public_name), f"{host}: public_hostname is missing/placeholder")
        problems.require(not is_placeholder(api_host), f"{host}: xray_api_public_host is missing/placeholder")
        public_port = parse_port(values.get("public_port", 443), f"{host} public", problems)
        api_port = parse_port(values.get("xray_api_port", 10085), f"{host} API", problems)
        metrics_port = parse_port(values.get("xray_metrics_port", 11111), f"{host} metrics", problems)
        ssh_port = parse_port(values.get("ansible_port", 22), f"{host} SSH", problems)
        mask_port = parse_port(values.get("mask_port", 8443), f"{host} mask", problems)
        node_exporter_port = parse_port(values.get("node_exporter_port", 9100), f"{host} node exporter", problems)
        xray_listen_port = parse_port(
            values.get("xray_listen_port", public_port), f"{host} Xray listener", problems
        )
        problems.require(
            public_port == xray_listen_port,
            f"{host}: public_port and xray_listen_port must be identical",
        )
        mask_bind_address = str(values.get("mask_bind_address", "127.0.0.1"))
        problems.require(
            mask_bind_address in {"127.0.0.1", "::1"},
            f"{host}: mask_bind_address must stay loopback-only",
        )
        expected_mask_target = f"{mask_bind_address}:{mask_port}" if mask_port else ""
        reality_dest = str(values.get("reality_dest", expected_mask_target))
        mask_listen = str(values.get("mask_listen", expected_mask_target))
        problems.require(
            reality_dest == expected_mask_target,
            f"{host}: reality_dest must equal {expected_mask_target}",
        )
        problems.require(
            mask_listen == expected_mask_target,
            f"{host}: mask_listen must equal {expected_mask_target}",
        )
        require_unique_ports(
            {
                "SSH": ssh_port,
                "VLESS": public_port,
                "Xray API": api_port,
                "Xray diagnostics": metrics_port,
                "mask": mask_port,
                "node_exporter": node_exporter_port,
            },
            host,
            problems,
        )
        if public_port and api_port:
            problems.require(public_port != api_port, f"{host}: public and API ports must differ")
        if api_port and metrics_port:
            problems.require(api_port != metrics_port, f"{host}: API and metrics ports must differ")
            endpoint = (api_host, api_port)
            problems.require(endpoint not in api_endpoints, f"{host}: duplicate API endpoint {api_host}:{api_port}")
            api_endpoints.add(endpoint)
        problems.require(public_name not in public_names, f"{host}: duplicate public hostname {public_name}")
        public_names.add(public_name)
        public_addresses.add(ansible_host)

        server_names = [str(item) for item in values.get("reality_server_names", [])]
        short_ids = [str(item) for item in values.get("reality_short_ids", [])]
        problems.require(bool(server_names), f"{host}: reality_server_names is empty")
        problems.require(all(name.strip() for name in server_names), f"{host}: REALITY server name cannot be empty")
        problems.require(bool(short_ids), f"{host}: reality_short_ids is empty")
        problems.require(len(short_ids) == len(set(short_ids)), f"{host}: duplicate REALITY short IDs")
        for short_id in short_ids:
            problems.require(
                bool(re.fullmatch(r"(?:[0-9a-fA-F]{2}){0,8}", short_id)),
                f"{host}: REALITY short ID must contain an even number of hex digits (0..16): {short_id!r}",
            )
        supplied_private = str(values.get("reality_private_key", "") or "").strip()
        if supplied_private:
            problems.require(
                bool(re.fullmatch(r"[A-Za-z0-9_-]{43}", supplied_private)),
                f"{host}: supplied REALITY private key is malformed",
            )

        mask_cert = materialize_file_lookup(
            values.get("mask_tls_certificate", ""), f"{host} mask certificate", problems
        )
        mask_key = materialize_file_lookup(
            values.get("mask_tls_private_key", ""), f"{host} mask private key", problems
        )
        validate_pem_pair(
            str(mask_cert),
            str(mask_key),
            f"{host} mask TLS",
            problems,
            server_names,
        )

        if not args.skip_dns and public_name and not is_placeholder(public_name):
            resolved = resolve_hostname(public_name)
            problems.require(bool(resolved), f"{host}: public hostname {public_name} does not resolve")
            ansible_ids = address_identities(ansible_host)
            problems.require(bool(ansible_ids), f"{host}: ansible_host {ansible_host} does not resolve")
            problems.require(
                bool(resolved.intersection(ansible_ids)),
                f"{host}: {public_name} does not identify ansible_host {ansible_host}",
            )

            api_ids = address_identities(api_host)
            problems.require(bool(api_ids), f"{host}: Xray API host {api_host} does not resolve")
            problems.require(
                bool(api_ids.intersection(ansible_ids)),
                f"{host}: xray_api_public_host {api_host} does not identify ansible_host {ansible_host}",
            )
        elif args.skip_dns:
            ansible_ip = literal_ip(ansible_host)
            api_ip = literal_ip(api_host)
            if ansible_ip is not None and api_ip is not None:
                problems.require(
                    ansible_ip == api_ip,
                    f"{host}: xray_api_public_host IP must equal ansible_host IP",
                )

        if role == "exit":
            country = str(values.get("country", ""))
            tag = f"{country}-exit" if country else ""
            problems.require(bool(re.fullmatch(r"[a-z0-9-]+", country)), f"{host}: invalid country code/tag component")
            problems.require(tag not in active_tags, f"{host}: duplicate exit tag {tag}")
            active_tags.add(tag)
            raw_uuid = str(values.get("entry_service_uuid", ""))
            try:
                parsed_uuid = str(uuid.UUID(raw_uuid))
            except ValueError:
                problems.require(False, f"{host}: entry_service_uuid is invalid")
            else:
                problems.require(parsed_uuid not in service_uuids, f"{host}: duplicate entry_service_uuid")
                service_uuids.add(parsed_uuid)
            expected_ip = str(values.get("expected_egress_ip") or ansible_host)
            problems.require(not is_placeholder(expected_ip), f"{host}: expected_egress_ip is missing/placeholder")
            try:
                ipaddress.ip_address(expected_ip)
            except ValueError:
                problems.require(False, f"{host}: expected_egress_ip is not an IP address")

    for host in entries:
        default_tag = str(hostvars[host].get("entry_default_exit_tag", ""))
        problems.require(default_tag in active_tags, f"{host}: entry_default_exit_tag {default_tag!r} has no enabled exit")
        problems.require(
            not bool(hostvars[host].get("xray_entry_block_unmatched", True)),
            f"{host}: normal API-created users would be blocked",
        )

    if platform:
        host = platform[0]
        values = hostvars[host]
        control = str(values.get("control_plane_host", ""))
        platform_address = str(values.get("ansible_host", ""))
        problems.require(not is_placeholder(control), f"{host}: control_plane_host is missing/placeholder")
        problems.require(not is_placeholder(platform_address), f"{host}: ansible_host is missing/placeholder")
        control_ids = address_identities(control)
        platform_ids = address_identities(platform_address)
        problems.require(bool(control_ids), f"{host}: control_plane_host does not resolve")
        problems.require(bool(platform_ids), f"{host}: platform ansible_host does not resolve")
        problems.require(
            bool(control_ids.intersection(platform_ids)),
            f"{host}: control_plane_host and platform ansible_host do not identify the same server",
        )
        problems.require(
            values.get("platform_bind_address") in {"0.0.0.0", "::"},
            f"{host}: platform services must be reachable by fleet nodes",
        )
        problems.require(
            values.get("vault_bind_address") in {"127.0.0.1", "::1"},
            f"{host}: Vault must remain localhost-bound",
        )
        grafana_password = materialize_file_lookup(
            values.get("grafana_admin_password", ""), f"{host} Grafana password", problems
        )
        problems.require(
            len(str(grafana_password)) >= 20 and not is_placeholder(grafana_password),
            f"{host}: Grafana password must be at least 20 non-placeholder characters",
        )
        for key, title, default in (
            ("loki_image", "Loki", "grafana/loki:3.7.3"),
            ("prometheus_image", "Prometheus", "prom/prometheus:v3.5.0"),
            ("alertmanager_image", "Alertmanager", "prom/alertmanager:v0.28.1"),
            ("grafana_image", "Grafana", "grafana/grafana:12.0.2"),
            ("blackbox_image", "blackbox exporter", "prom/blackbox-exporter:v0.27.0"),
            ("alloy_image", "Alloy", "grafana/alloy:v1.17.1"),
            ("node_exporter_image", "node_exporter", "quay.io/prometheus/node-exporter:v1.9.1"),
            ("vault_image", "Vault", "hashicorp/vault:2.0.3"),
        ):
            validate_image_reference(values.get(key, default), f"{host} {title}", problems)
        loki_port = parse_port(values.get("loki_port", 3100), f"{host} Loki", problems)
        prometheus_port = parse_port(values.get("prometheus_port", 9090), f"{host} Prometheus", problems)
        alertmanager_port = parse_port(values.get("alertmanager_port", 9093), f"{host} Alertmanager", problems)
        grafana_port = parse_port(values.get("grafana_port", 3000), f"{host} Grafana", problems)
        vault_api_port = parse_port(values.get("vault_api_port", 8200), f"{host} Vault API", problems)
        vault_cluster_port = parse_port(values.get("vault_cluster_port", 8201), f"{host} Vault cluster", problems)
        ssh_port = parse_port(values.get("ansible_port", 22), f"{host} SSH", problems)
        require_unique_ports(
            {
                "SSH": ssh_port,
                "Loki": loki_port,
                "Prometheus": prometheus_port,
                "Alertmanager": alertmanager_port,
                "Grafana": grafana_port,
                "Vault API": vault_api_port,
                "Vault cluster": vault_cluster_port,
            },
            host,
            problems,
        )
        if loki_port:
            for node in fleet:
                validate_http_endpoint(
                    hostvars[node].get("loki_ops_endpoint"),
                    control,
                    loki_port,
                    "/loki/api/v1/push",
                    f"{node} Loki push endpoint",
                    problems,
                )
        if prometheus_port:
            for node in fleet:
                validate_http_endpoint(
                    hostvars[node].get("prometheus_remote_write"),
                    control,
                    prometheus_port,
                    "/api/v1/write",
                    f"{node} Prometheus remote-write endpoint",
                    problems,
                )
        if bool(values.get("vault_tls_enabled", False)):
            vault_cert = materialize_file_lookup(
                values.get("vault_tls_certificate", ""), f"{host} Vault certificate", problems
            )
            vault_key = materialize_file_lookup(
                values.get("vault_tls_private_key", ""), f"{host} Vault private key", problems
            )
            validate_pem_pair(
                str(vault_cert),
                str(vault_key),
                f"{host} Vault TLS",
                problems,
            )

    problems.require(len(public_addresses) == len(fleet), "active VPN nodes must have unique ansible_host addresses")
    problems.finish()
    print(f"Inventory preflight passed: platform={len(platform)} entries={len(entries)} exits={len(exits)}")


if __name__ == "__main__":
    main()
