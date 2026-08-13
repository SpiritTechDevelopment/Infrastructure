from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MARKDOWN_LINK = re.compile(r"\[[^]]*\]\(([^)]+)\)")


class DocumentationTests(unittest.TestCase):
    def test_normative_v1_documents_exist(self) -> None:
        required = (
            "docs/architecture/INFRA_TECHNICAL_SPEC.md",
            "docs/status/INFRA_V1_IMPLEMENTATION_STATUS.md",
            "contracts/backend/BACKEND_DOMAIN_AGREEMENTS.md",
            "contracts/nodeagent/v1/node_agent.proto",
            "fleetctl/README.md",
            "desired/README.md",
        )
        for relative in required:
            with self.subTest(path=relative):
                self.assertTrue((REPO_ROOT / relative).is_file())

    def test_legacy_documentation_trees_are_absent(self) -> None:
        removed = (
            ".local-secrets.example",
            "captured-state",
            "docs/archive",
            "docs/deploy",
            "docs/reference",
            "docs/security",
            "docs/testing",
            "governance",
        )
        for relative in removed:
            with self.subTest(path=relative):
                directory = REPO_ROOT / relative
                self.assertFalse(
                    directory.exists() and any(item.is_file() for item in directory.rglob("*"))
                )

    def test_local_markdown_links_resolve(self) -> None:
        missing: list[str] = []
        tracked = subprocess.check_output(
            ["git", "ls-files", "-z", "--", "*.md"], cwd=REPO_ROOT
        ).split(b"\0")
        for relative in sorted(item.decode("utf-8") for item in tracked if item):
            path = REPO_ROOT / relative
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for raw_target in MARKDOWN_LINK.findall(line):
                    target = raw_target.split("#", 1)[0].strip()
                    if not target or "://" in target or target.startswith("mailto:"):
                        continue
                    if not (path.parent / target).resolve().exists():
                        missing.append(f"{path.relative_to(REPO_ROOT)}:{line_number}: {raw_target}")
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
