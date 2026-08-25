from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MARKDOWN_LINK = re.compile(r"\[[^]]*\]\(([^)]+)\)")


class DocumentationTests(unittest.TestCase):
    # Список проверяет только существование файла, поэтому в нём остаётся то,
    # на что ссылается код или контракт, а не то, чему хочется придать статус.
    def test_referenced_documents_exist(self) -> None:
        required = (
            "docs/ARCHITECTURE.md",
            "contracts/README.md",
            "contracts/nodeagent/v1/node_agent.proto",
            "fleetctl/README.md",
            "desired/README.md",
        )
        for relative in required:
            with self.subTest(path=relative):
                self.assertTrue((REPO_ROOT / relative).is_file())

    def test_superseded_documentation_trees_are_absent(self) -> None:
        removed = (
            ".local-secrets.example",
            "captured-state",
            "contracts/backend",
            "dev",
            "examples",
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
            # Индекс перечисляет и файл, удалённый в рабочем дереве, но ещё не
            # снятый с учёта. Ссылок в нём читать нечего, а падение здесь
            # маскировало бы настоящие висячие ссылки в остальных файлах.
            if not path.is_file():
                continue
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
