"""Fail-closed сверка DNS Cloudflare со скомпилированными записями флота."""

from __future__ import annotations

import ipaddress
import json
import os
import stat
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


DEFAULT_API_BASE = "https://api.cloudflare.com/client/v4"


class CloudflareDnsError(Exception):
    """Cloudflare отклонил запрос либо скомпилированный план небезопасен."""


def read_cloudflare_token(token_file: Path | None) -> str:
    """Read a token without accepting a loose or redirected credential file."""

    if token_file is None:
        token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
        if not token:
            raise CloudflareDnsError("provide --token-file or set CLOUDFLARE_API_TOKEN")
        return token
    if token_file.is_symlink():
        raise CloudflareDnsError(f"refusing symlink Cloudflare token file: {token_file}")
    metadata = token_file.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise CloudflareDnsError(f"Cloudflare token path is not a regular file: {token_file}")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise CloudflareDnsError(
            f"Cloudflare token file must not be group/world accessible: {token_file}"
        )
    token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise CloudflareDnsError(f"Cloudflare token file is empty: {token_file}")
    return token


class DnsRecordClient(Protocol):
    def zone_id(self, zone: str) -> str: ...

    def records(self, zone_id: str, record_type: str, name: str) -> list[dict[str, Any]]: ...

    def create_record(self, zone_id: str, payload: dict[str, Any]) -> None: ...

    def update_record(self, zone_id: str, record_id: str, payload: dict[str, Any]) -> None: ...


@dataclass(slots=True)
class CloudflareClient:
    token: str
    api_base: str = DEFAULT_API_BASE
    timeout_seconds: int = 20

    def __post_init__(self) -> None:
        self.token = self.token.strip()
        if not self.token:
            raise CloudflareDnsError("Cloudflare API token is empty")
        self.api_base = self.api_base.rstrip("/")

    def zone_id(self, zone: str) -> str:
        response = self._request("GET", "/zones", query={"name": zone})
        result = response.get("result")
        if not isinstance(result, list) or len(result) != 1:
            raise CloudflareDnsError(
                f"Cloudflare zone {zone!r} was not resolved uniquely; check token scope and zone name"
            )
        zone_id = result[0].get("id") if isinstance(result[0], dict) else None
        if not isinstance(zone_id, str) or not zone_id:
            raise CloudflareDnsError(f"Cloudflare zone {zone!r} returned no ID")
        if result[0].get("status") != "active" or result[0].get("paused") is True:
            raise CloudflareDnsError(f"Cloudflare zone {zone!r} is not active")
        return zone_id

    def records(self, zone_id: str, record_type: str, name: str) -> list[dict[str, Any]]:
        response = self._request(
            "GET",
            f"/zones/{urllib.parse.quote(zone_id, safe='')}/dns_records",
            query={"type": record_type, "name": name, "per_page": "100"},
        )
        result = response.get("result")
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise CloudflareDnsError(f"Cloudflare returned malformed records for {name!r}")
        return result

    def create_record(self, zone_id: str, payload: dict[str, Any]) -> None:
        self._request(
            "POST",
            f"/zones/{urllib.parse.quote(zone_id, safe='')}/dns_records",
            body=payload,
        )

    def update_record(self, zone_id: str, record_id: str, payload: dict[str, Any]) -> None:
        self._request(
            "PATCH",
            (
                f"/zones/{urllib.parse.quote(zone_id, safe='')}/dns_records/"
                f"{urllib.parse.quote(record_id, safe='')}"
            ),
            body=payload,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self.api_base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url,
            data=payload,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                document = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:2000]
            raise CloudflareDnsError(
                f"Cloudflare API {method} {path} returned HTTP {exc.code}: {detail}"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise CloudflareDnsError(f"Cloudflare API {method} {path} is unreachable: {exc}") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CloudflareDnsError(
                f"Cloudflare API {method} {path} returned malformed JSON"
            ) from exc
        if not isinstance(document, dict) or document.get("success") is not True:
            errors = document.get("errors") if isinstance(document, dict) else None
            raise CloudflareDnsError(f"Cloudflare API {method} {path} failed: {errors!r}")
        return document


def reconcile_cloudflare_dns(
    plan: dict[str, Any],
    client: DnsRecordClient,
    *,
    apply: bool,
) -> dict[str, Any]:
    """Планирует или применяет создание и обновление без удаления записей."""

    zone, desired = _validated_records(plan)
    zone_id = client.zone_id(zone)
    results: list[dict[str, Any]] = []
    mutations: list[tuple[str, str | None, dict[str, Any]]] = []
    for record in desired:
        payload = {
            "type": record["record_type"],
            "name": record["name"],
            "content": record["value"],
            "ttl": record["ttl_seconds"],
            "proxied": False,
        }
        current = client.records(zone_id, payload["type"], payload["name"])
        if len(current) > 1:
            raise CloudflareDnsError(
                f"multiple Cloudflare {payload['type']} records exist for {payload['name']!r}"
            )
        if not current:
            action = "create"
            before = None
            mutations.append((action, None, payload))
        else:
            existing = current[0]
            before = {
                "value": existing.get("content"),
                "ttl_seconds": existing.get("ttl"),
                "proxied": existing.get("proxied"),
            }
            drifted = before != {
                "value": payload["content"],
                "ttl_seconds": payload["ttl"],
                "proxied": payload["proxied"],
            }
            action = "update" if drifted else "unchanged"
            if drifted:
                record_id = existing.get("id")
                if not isinstance(record_id, str) or not record_id:
                    raise CloudflareDnsError(
                        f"Cloudflare record {payload['name']!r} returned no ID"
                    )
                mutations.append((action, record_id, payload))
        results.append(
            {
                "action": action,
                "name": payload["name"],
                "record_type": payload["type"],
                "before": before,
                "desired": {
                    "value": payload["content"],
                    "ttl_seconds": payload["ttl"],
                    "proxied": payload["proxied"],
                },
            }
        )
    if apply:
        for action, record_id, payload in mutations:
            if action == "create":
                client.create_record(zone_id, payload)
            else:
                assert record_id is not None
                client.update_record(zone_id, record_id, payload)
    return {
        "schema_version": 1,
        "environment": plan.get("environment"),
        "zone": zone,
        "mode": "apply" if apply else "plan",
        "change_count": sum(item["action"] != "unchanged" for item in results),
        "records": results,
    }


def _validated_records(plan: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    if plan.get("schema_version") != 1:
        raise CloudflareDnsError("unsupported DNS plan schema version")
    zone = plan.get("zone")
    records = plan.get("records")
    if not isinstance(zone, str) or not zone:
        raise CloudflareDnsError("DNS plan zone is required")
    if not isinstance(records, list):
        raise CloudflareDnsError("DNS plan records must be an array")
    seen: set[tuple[str, str]] = set()
    validated: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise CloudflareDnsError(f"DNS record {index} must be an object")
        name = record.get("name")
        record_type = record.get("record_type")
        value = record.get("value")
        ttl = record.get("ttl_seconds")
        if not isinstance(name, str) or not (name == zone or name.endswith("." + zone)):
            raise CloudflareDnsError(f"DNS record {index} is outside zone {zone!r}")
        if record_type not in ("A", "AAAA"):
            raise CloudflareDnsError(f"DNS record {name!r} has unsupported type {record_type!r}")
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise CloudflareDnsError(f"DNS record {name!r} has invalid address") from exc
        if (record_type == "A") != (address.version == 4):
            raise CloudflareDnsError(f"DNS record {name!r} type does not match its address")
        if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= 0:
            raise CloudflareDnsError(f"DNS record {name!r} has invalid TTL")
        if record.get("proxied") is not False:
            raise CloudflareDnsError(f"DNS record {name!r} must remain DNS-only")
        identity = (record_type, name)
        if identity in seen:
            raise CloudflareDnsError(f"DNS plan contains duplicate {record_type} record {name!r}")
        seen.add(identity)
        validated.append(record)
    return zone, validated
