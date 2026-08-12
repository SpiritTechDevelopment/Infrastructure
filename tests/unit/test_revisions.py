from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fleetctl.deployment import ManifestRevisionAllocator, RevisionStateError


class ManifestRevisionAllocatorTests(unittest.TestCase):
    def allocate(
        self,
        path: Path,
        environment: str,
        deployment_id: str,
        digest: str,
    ) -> int:
        return ManifestRevisionAllocator(path, environment).allocate(
            deployment_id=deployment_id,
            source_git_sha="a" * 40,
            payload_digest=digest,
            allow_destructive=False,
        ).revision

    def test_sequences_are_independent_per_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            develop = root / "develop.json"
            prod = root / "prod.json"

            self.assertEqual(self.allocate(develop, "develop", "develop-a", "sha256:" + "1" * 64), 1)
            self.assertEqual(self.allocate(develop, "develop", "develop-b", "sha256:" + "2" * 64), 2)
            self.assertEqual(self.allocate(prod, "prod", "prod-a", "sha256:" + "3" * 64), 1)

    def test_same_deployment_reuses_revision_only_for_same_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "develop.json"
            first = self.allocate(path, "develop", "develop-a", "sha256:" + "1" * 64)
            repeated = self.allocate(path, "develop", "develop-a", "sha256:" + "1" * 64)
            with self.assertRaisesRegex(RevisionStateError, "conflicts"):
                self.allocate(path, "develop", "develop-a", "sha256:" + "2" * 64)

        self.assertEqual(first, repeated)

    def test_pinned_deployment_requires_its_existing_allocation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "develop.json"
            self.allocate(path, "develop", "develop-a", "sha256:" + "1" * 64)

            with self.assertRaisesRegex(RevisionStateError, "allocation is missing"):
                ManifestRevisionAllocator(path, "develop").allocate(
                    deployment_id="develop-b",
                    source_git_sha="b" * 40,
                    payload_digest="sha256:" + "2" * 64,
                    allow_destructive=False,
                    require_existing_allocation=True,
                )

    def test_corrupt_or_cross_environment_state_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            path.write_text("not-json\n", encoding="utf-8")
            with self.assertRaisesRegex(RevisionStateError, "unreadable"):
                self.allocate(path, "develop", "develop-a", "sha256:" + "1" * 64)

            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "environment": "prod",
                        "last_allocated_revision": 0,
                        "allocations": {},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RevisionStateError, "another schema or environment"):
                self.allocate(path, "develop", "develop-a", "sha256:" + "1" * 64)


if __name__ == "__main__":
    unittest.main()
