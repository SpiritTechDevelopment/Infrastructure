#!/usr/bin/env python3
"""Resolve environment-scoped secret:// references on the management executor."""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from fleetctl.validation import DesiredStateInvalid, validate_environment


REFERENCE = re.compile(
    r"^secret://kv/(develop|prod)/([A-Za-z0-9._/-]+)#([A-Za-z_][A-Za-z0-9_]*)$"
)


class ResolverError(Exception):
    pass


def parse_reference(reference: str, environment: str) -> tuple[str, str]:
    match = REFERENCE.fullmatch(reference)
    if match is None:
        raise ResolverError(f"invalid secret reference: {reference}")
    reference_environment, path, field = match.groups()
    if reference_environment != environment:
        raise ResolverError("cross-environment secret reference refused")
    if "//" in f"/{path}/" or ".." in path.split("/"):
        raise ResolverError("unsafe secret path refused")
    return f"{environment}/{path}", field


def desired_references(
    root: Path, environment: str, desired_root: Path | None = None
) -> list[str]:
    state = validate_environment(root, environment, desired_root=desired_root)
    references = {
        reference
        for node in state.nodes
        for reference in (
            node.private_key_ref,
            node.mask_certificate_ref,
            node.mask_private_key_ref,
        )
    }
    references.update(
        bridge.service_credential_ref
        for fleet in state.fleets
        for bridge in fleet.bridges
    )
    return sorted(references)


class VaultClient:
    def __init__(self, address: str, ca_file: Path, role_id: str, secret_id: str):
        self.address = address.rstrip("/")
        self.context = ssl.create_default_context(cafile=str(ca_file))
        response = self._request(
            "POST",
            "/v1/auth/approle/login",
            {"role_id": role_id, "secret_id": secret_id},
        )
        try:
            self.token = response["auth"]["client_token"]
        except (KeyError, TypeError) as exc:
            raise ResolverError("Vault AppRole login returned no client token") from exc

    def _request(self, method: str, path: str, payload: dict[str, str] | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        token = getattr(self, "token", None)
        if token is not None:
            headers["X-Vault-Token"] = token
        request = urllib.request.Request(
            f"{self.address}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=10) as response:
                value = json.load(response)
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise ResolverError(f"Vault request failed for {path}") from exc
        if not isinstance(value, dict):
            raise ResolverError(f"Vault returned an invalid response for {path}")
        return value

    def read(self, path: str, field: str) -> str:
        response = self._request("GET", f"/v1/kv/data/{path}")
        try:
            value = response["data"]["data"][field]
        except (KeyError, TypeError) as exc:
            raise ResolverError(f"Vault field is missing: kv/{path}#{field}") from exc
        if not isinstance(value, str) or not value:
            raise ResolverError(f"Vault field must be a non-empty string: kv/{path}#{field}")
        return value


def write_private(path: Path, value: str) -> None:
    if path.is_symlink():
        raise ResolverError(f"refusing symlink output: {path}")
    path = path.absolute()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--desired-root", type=Path, help="explicit desired/ fixture (tests only)")
    parser.add_argument("--environment", required=True, choices=("develop", "prod"))
    parser.add_argument("--credentials-dir", type=Path)
    parser.add_argument("--compiled-secrets", type=Path)
    parser.add_argument("--ssh-private-key", type=Path)
    parser.add_argument("--list-references", action="store_true")
    parser.add_argument("--vault-address", default="https://127.0.0.1:8200")
    parser.add_argument(
        "--vault-ca",
        type=Path,
        default=Path("/opt/spiritvpn/platform/vault/tls/vault-ca.crt"),
    )
    args = parser.parse_args()
    try:
        references = desired_references(args.root, args.environment, args.desired_root)
        ssh_reference = f"secret://kv/{args.environment}/executor/ansible#private_key"
        if args.list_references:
            for reference in (*references, ssh_reference):
                print(reference)
            return 0
        if args.credentials_dir is None or args.compiled_secrets is None or args.ssh_private_key is None:
            raise ResolverError(
                "resolution requires --credentials-dir, --compiled-secrets, and --ssh-private-key"
            )
        role_id = (args.credentials_dir / "role-id").read_text(encoding="utf-8").strip()
        secret_id = (args.credentials_dir / "secret-id").read_text(encoding="utf-8").strip()
        if not role_id or not secret_id:
            raise ResolverError("Vault AppRole credentials are empty")
        client = VaultClient(args.vault_address, args.vault_ca, role_id, secret_id)
        resolved = {}
        for reference in references:
            path, field = parse_reference(reference, args.environment)
            resolved[reference] = client.read(path, field)
        ssh_path, ssh_field = parse_reference(ssh_reference, args.environment)
        ssh_private_key = client.read(ssh_path, ssh_field)
        if "PRIVATE KEY" not in ssh_private_key:
            raise ResolverError("executor Ansible private key has an invalid format")
        payload = yaml.safe_dump(
            {"spiritvpn_secret_values": resolved},
            allow_unicode=True,
            sort_keys=True,
        )
        write_private(args.compiled_secrets, payload)
        write_private(args.ssh_private_key, ssh_private_key.rstrip("\n") + "\n")
    except (OSError, ResolverError, DesiredStateInvalid, ValueError) as exc:
        print(f"secret resolution failed: {exc}", file=sys.stderr)
        return 2
    print(f"{args.environment}: resolved {len(resolved)} desired-state secret reference(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
