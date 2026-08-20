from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fleetctl.adapters import CloudflareDnsError, reconcile_cloudflare_dns
from fleetctl.cli import _read_cloudflare_token


def dns_plan(*records: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "environment": "develop",
        "zone": "example.invalid",
        "records": list(records),
    }


def a_record(
    name: str,
    value: str,
    *,
    ttl: int = 60,
    proxied: bool = False,
) -> dict[str, Any]:
    return {
        "name": name,
        "record_type": "A",
        "value": value,
        "ttl_seconds": ttl,
        "proxied": proxied,
    }


class FakeDnsClient:
    def __init__(self, records: dict[tuple[str, str], list[dict[str, Any]]] | None = None) -> None:
        self.current = records or {}
        self.created: list[dict[str, Any]] = []
        self.updated: list[tuple[str, dict[str, Any]]] = []
        self.requested_zone: str | None = None

    def zone_id(self, zone: str) -> str:
        self.requested_zone = zone
        return "zone-id"

    def records(self, zone_id: str, record_type: str, name: str) -> list[dict[str, Any]]:
        self.assert_zone_id(zone_id)
        return self.current.get((record_type, name), [])

    def create_record(self, zone_id: str, payload: dict[str, Any]) -> None:
        self.assert_zone_id(zone_id)
        self.created.append(payload)

    def update_record(self, zone_id: str, record_id: str, payload: dict[str, Any]) -> None:
        self.assert_zone_id(zone_id)
        self.updated.append((record_id, payload))

    @staticmethod
    def assert_zone_id(zone_id: str) -> None:
        if zone_id != "zone-id":
            raise AssertionError(f"unexpected zone ID: {zone_id}")


class CloudflareDnsTests(unittest.TestCase):
    def test_plan_reports_create_without_mutating(self) -> None:
        client = FakeDnsClient()
        result = reconcile_cloudflare_dns(
            dns_plan(a_record("ro.example.invalid", "192.0.2.20")),
            client,
            apply=False,
        )

        self.assertEqual(result["mode"], "plan")
        self.assertEqual(result["change_count"], 1)
        self.assertEqual(result["records"][0]["action"], "create")
        self.assertEqual(client.requested_zone, "example.invalid")
        self.assertEqual(client.created, [])
        self.assertEqual(client.updated, [])

    def test_apply_creates_updates_and_leaves_matching_records_unchanged(self) -> None:
        client = FakeDnsClient(
            {
                ("A", "keep.example.invalid"): [
                    {
                        "id": "keep-id",
                        "content": "192.0.2.10",
                        "ttl": 60,
                        "proxied": False,
                    }
                ],
                ("A", "move.example.invalid"): [
                    {
                        "id": "move-id",
                        "content": "192.0.2.99",
                        "ttl": 1,
                        "proxied": True,
                    }
                ],
            }
        )
        result = reconcile_cloudflare_dns(
            dns_plan(
                a_record("create.example.invalid", "192.0.2.30"),
                a_record("keep.example.invalid", "192.0.2.10"),
                a_record("move.example.invalid", "192.0.2.20"),
            ),
            client,
            apply=True,
        )

        self.assertEqual(result["mode"], "apply")
        self.assertEqual(result["change_count"], 2)
        self.assertEqual(
            [record["action"] for record in result["records"]],
            ["create", "unchanged", "update"],
        )
        self.assertEqual(client.created[0]["name"], "create.example.invalid")
        self.assertEqual(client.updated[0][0], "move-id")
        self.assertEqual(client.updated[0][1]["proxied"], False)
        self.assertEqual(client.updated[0][1]["ttl"], 60)

    def test_refuses_records_outside_zone_or_proxied(self) -> None:
        for record in (
            a_record("outside.test", "192.0.2.20"),
            a_record("ro.example.invalid", "192.0.2.20", proxied=True),
        ):
            with self.subTest(record=record):
                with self.assertRaises(CloudflareDnsError):
                    reconcile_cloudflare_dns(dns_plan(record), FakeDnsClient(), apply=False)

    def test_refuses_duplicate_or_address_type_mismatch(self) -> None:
        record = a_record("ro.example.invalid", "192.0.2.20")
        with self.assertRaisesRegex(CloudflareDnsError, "duplicate"):
            reconcile_cloudflare_dns(dns_plan(record, record), FakeDnsClient(), apply=False)

        mismatch = {**record, "record_type": "AAAA"}
        with self.assertRaisesRegex(CloudflareDnsError, "does not match"):
            reconcile_cloudflare_dns(dns_plan(mismatch), FakeDnsClient(), apply=False)

    def test_refuses_ambiguous_provider_records(self) -> None:
        client = FakeDnsClient(
            {
                ("A", "ro.example.invalid"): [
                    {"id": "one"},
                    {"id": "two"},
                ]
            }
        )
        with self.assertRaisesRegex(CloudflareDnsError, "multiple"):
            reconcile_cloudflare_dns(
                dns_plan(a_record("ro.example.invalid", "192.0.2.20")),
                client,
                apply=True,
            )

    def test_validates_all_provider_state_before_mutating(self) -> None:
        client = FakeDnsClient(
            {
                ("A", "ambiguous.example.invalid"): [
                    {"id": "one"},
                    {"id": "two"},
                ]
            }
        )
        with self.assertRaisesRegex(CloudflareDnsError, "multiple"):
            reconcile_cloudflare_dns(
                dns_plan(
                    a_record("create.example.invalid", "192.0.2.10"),
                    a_record("ambiguous.example.invalid", "192.0.2.20"),
                ),
                client,
                apply=True,
            )
        self.assertEqual(client.created, [])

    def test_token_file_must_be_private_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            token_path = Path(temporary_directory) / "cloudflare-token"
            token_path.write_text(" secret-token\n", encoding="utf-8")
            token_path.chmod(0o600)
            self.assertEqual(_read_cloudflare_token(token_path), "secret-token")

            token_path.chmod(0o640)
            with self.assertRaisesRegex(CloudflareDnsError, "group/world"):
                _read_cloudflare_token(token_path)

    def test_token_can_be_read_from_environment(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": " token "}):
            self.assertEqual(_read_cloudflare_token(None), "token")


if __name__ == "__main__":
    unittest.main()
