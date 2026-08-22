from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


class RepositorySurfaceTests(unittest.TestCase):
    def test_only_v1_deployment_surfaces_are_tracked(self) -> None:
        tracked = {
            path
            for path in subprocess.check_output(
                ["git", "ls-files"], cwd=REPO_ROOT, text=True
            ).splitlines()
            if (REPO_ROOT / path).exists()
        }
        forbidden_roots = (
            "inventories/prod/",
            "inventories/dev/",
            "roles/acme/",
            "roles/alloy/",
            "roles/backend/",
            "roles/cloudflare_dns/",
            "roles/management_wireguard/",
            "roles/observability/",
            "roles/vault/",
            "roles/vpn_stack/",
        )
        unexpected = sorted(
            path for path in tracked if any(path.startswith(root) for root in forbidden_roots)
        )
        self.assertEqual(unexpected, [])

    def test_makefile_exposes_only_declared_operation_families(self) -> None:
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        targets = set(re.findall(r"(?m)^([a-zA-Z0-9_-]+):", makefile))
        operational = targets - {"help", "check", "lint", "syntax"}
        self.assertTrue(operational)
        # Семейств два, и они разделены намеренно. `fleet-` трогает серверы.
        # `operator-` не трогает их вовсе: выдача и отзыв доступа правят только
        # объявления в Git и оставляют диффф под ревью, а применяет его отдельная
        # защищённая операция. Общий префикс скрыл бы эту разницу, а список без
        # префиксов перестал бы быть ограничителем.
        families = ("fleet-", "operator-")
        unexpected = sorted(
            target for target in operational if not target.startswith(families)
        )
        self.assertEqual(unexpected, [], "цель вне объявленных семейств операций")
        self.assertNotIn("ALLOW_LEGACY", makefile)

    def test_scripts_are_bounded_to_the_v1_control_plane(self) -> None:
        scripts = {
            path.name
            for path in (REPO_ROOT / "scripts").iterdir()
            if path.is_file() and path.name != "__pycache__"
        }
        self.assertEqual(
            scripts,
            {
                "platform-bootstrap-check.py",
                "platform-bootstrap.sh",
                "bootstrap-platform.py",
                # Projects immutable platform images from the canonical SOPS
                # common desired state instead of role-local defaults.
                "platform-component-vars.py",
                # Compares the exact-SHA compiled backup policy with the
                # explicitly approved local contract without printing argv.
                "control-contract-check.py",
                "platform-sops.py",
                # Личность оператора создаётся на его собственной машине:
                # приватные ключи не покидают её, наружу уходит только запрос
                # с публичными частями и один отпечаток для сверки вне канала.
                "operator-identity.py",
                # Выдача и отзыв правят только объявления — ростер в контракте
                # платформы и получателей в .sops.yaml — и перешифровывают
                # затронутые файлы. Ничего не применяют и на серверы не ходят.
                "operator-access.py",
                "platform-remote.sh",
                "bootstrap-self-hosted-runner.sh",
                # Creates the one decryption identity intentionally assigned to
                # the dedicated runner; the private half never leaves it.
                "bootstrap-runner-sops.sh",
                # Joins that runner to the management overlay; the hub is only
                # reachable there, so registering the runner is part of the
                # same bootstrap as installing it.
                "enroll-runner-overlay.sh",
                "vault-secret-resolver.py",
                "vendor-backend-contract.sh",
                # Read-only triage of a deployment run. Lives here because the
                # job-level log endpoint is the only one reachable from the
                # workstation, and because a green PLAY RECAP on a red run means
                # the coordinator failed between steps — a place nobody looks.
                "deploy-log.sh",
                # Reads the executor transcript and answers one question: may the
                # deployment ref advance. Separate from the workflow because the
                # answer decides a write to the repository and has to be testable
                # without a deployment.
                "deployment-record.py",
                # Packs and updates the encrypted environment bundle while
                # keeping plaintext in process memory.
                "topology-release.py",
                # Public CI can validate ciphertext structure and coverage
                # without receiving any age identity.
                "sops-envelope-check.py",
            },
        )


if __name__ == "__main__":
    unittest.main()
