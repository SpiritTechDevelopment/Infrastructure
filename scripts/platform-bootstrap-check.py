#!/usr/bin/env python3
"""Fail-closed validation of the one allowed hand-maintained inventory."""

from __future__ import annotations

import argparse
import base64
import binascii
import ipaddress
import sys
from pathlib import Path

import yaml


PRIVATE_MANAGEMENT_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)


def is_allowed_management_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return address.is_global or any(address in network for network in PRIVATE_MANAGEMENT_NETWORKS)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--known-hosts", required=True, type=Path)
    args = parser.parse_args()
    try:
        document = yaml.safe_load(args.inventory.read_text(encoding="utf-8"))
        hosts = document["all"]["children"]["spiritvpn_platform_bootstrap"]["hosts"]
        if not isinstance(hosts, dict) or len(hosts) != 1:
            raise ValueError("bootstrap inventory must contain exactly one management host")
        host, variables = next(iter(hosts.items()))
        if not isinstance(variables, dict) or set(variables) != {"ansible_host", "ansible_user"}:
            raise ValueError("management host must contain only ansible_host and ansible_user")
        if variables["ansible_user"] != "root":
            raise ValueError("initial management bootstrap user must be root")
        address = ipaddress.ip_address(variables["ansible_host"])
        if not is_allowed_management_address(address):
            raise ValueError("management ansible_host must be a global or private tunnel IP address")
        known_hosts = [
            line.strip()
            for line in args.known_hosts.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not known_hosts:
            raise ValueError("known_hosts must contain an independently verified public host key")
        matching_key = False
        for line in known_hosts:
            fields = line.split()
            if len(fields) < 3 or fields[1] not in {"ssh-ed25519", "ecdsa-sha2-nistp256", "ssh-rsa"}:
                raise ValueError("known_hosts contains an invalid or unsupported public key line")
            try:
                base64.b64decode(fields[2], validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError("known_hosts contains invalid public key data") from exc
            matching_key |= str(address) in fields[0].split(",")
            matching_key |= f"[{address}]:22" in fields[0].split(",")
        if not matching_key:
            raise ValueError("known_hosts has no key for the management ansible_host")
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
        print(f"platform bootstrap input invalid: {exc}", file=sys.stderr)
        return 2
    print(f"platform bootstrap input valid: {host} ({address})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
