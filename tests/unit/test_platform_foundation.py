from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import yaml
from jinja2 import Environment

from fleetctl.validation import validate_environment


REPO_ROOT = Path(__file__).resolve().parents[2]
VALID_DESIRED = REPO_ROOT / "tests" / "fixtures" / "valid" / "desired"


class RecordingVault(BaseHTTPRequestHandler):
    """A stand-in Vault that records the calls the resolver actually makes."""

    calls: list[tuple[str, str, str | None]] = []
    fail_reads = False

    def log_message(self, *arguments: object) -> None:  # keep test output clean
        return

    def _respond(self, status: int, body: bytes = b"") -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        type(self).calls.append(("POST", self.path, self.headers.get("X-Vault-Token")))
        if self.path == "/v1/auth/approle/login":
            self._respond(200, json.dumps({"auth": {"client_token": "s.TEST"}}).encode())
        elif self.path == "/v1/auth/token/revoke-self":
            self._respond(204)  # Vault answers revocation with an empty body
        else:
            self._respond(404)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        type(self).calls.append(("GET", self.path, self.headers.get("X-Vault-Token")))
        if type(self).fail_reads:
            self._respond(500, b'{"errors":["unavailable"]}')
            return
        self._respond(
            200,
            json.dumps(
                {
                    "data": {
                        "data": {
                            "service_uuid": "0f9d3a2e-1111-2222-3333-444455556666",
                            "fullchain": "-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----",
                            "private_key": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----",
                        }
                    }
                }
            ).encode(),
        )


class PlatformFoundationTests(unittest.TestCase):
    def run_preflight(self, inventory: str, known_hosts: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory_path = root / "platform.yml"
            known_hosts_path = root / "known_hosts"
            inventory_path.write_text(inventory, encoding="utf-8")
            known_hosts_path.write_text(known_hosts, encoding="utf-8")
            return subprocess.run(
                [
                    "python3",
                    str(REPO_ROOT / "scripts" / "platform-bootstrap-check.py"),
                    "--inventory",
                    str(inventory_path),
                    "--known-hosts",
                    str(known_hosts_path),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

    def test_tracked_platform_bootstrap_is_sops_ciphertext_only(self) -> None:
        bootstrap = REPO_ROOT / "inventories" / "bootstrap"
        self.assertFalse((bootstrap / "platform.yml").exists())
        self.assertFalse((bootstrap / "known_hosts").exists())
        envelope = yaml.safe_load((bootstrap / "platform.sops.yml").read_text(encoding="utf-8"))
        self.assertEqual(envelope["apiVersion"], "spiritvpn.io/v1alpha1")
        self.assertEqual(envelope["kind"], "PlatformBootstrap")
        for field in ("inventory", "known_hosts", "vars"):
            self.assertTrue(envelope[field].startswith("ENC[AES256_GCM,"))
        ciphertext = (bootstrap / "platform.sops.yml").read_text(encoding="utf-8")
        for forbidden in ("ansible_host", "ssh-ed25519", "REPLACE_WITH", "github-develop"):
            self.assertNotIn(forbidden, ciphertext)

    def test_real_minimal_bootstrap_input_passes(self) -> None:
        fixture = REPO_ROOT / "tests" / "fixtures" / "platform-bootstrap"
        result = subprocess.run(
            [
                "python3",
                str(REPO_ROOT / "scripts" / "platform-bootstrap-check.py"),
                "--inventory",
                str(fixture / "platform.yml"),
                "--known-hosts",
                str(fixture / "known_hosts"),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("management-1 (1.1.1.1)", result.stdout)

    def test_private_wireguard_management_address_passes(self) -> None:
        result = self.run_preflight(
            """---
all:
  children:
    spiritvpn_platform_bootstrap:
      hosts:
        management-1:
          ansible_host: 10.70.0.1
          ansible_user: root
""",
            "10.70.0.1 ssh-ed25519 Zml4dHVyZS1wdWJsaWMta2V5\n",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_documentation_address_is_not_accepted_as_management(self) -> None:
        result = self.run_preflight(
            """---
all:
  children:
    spiritvpn_platform_bootstrap:
      hosts:
        management-1:
          ansible_host: 192.0.2.1
          ansible_user: root
""",
            "192.0.2.1 ssh-ed25519 Zml4dHVyZS1wdWJsaWMta2V5\n",
        )
        self.assertEqual(result.returncode, 2)

    def test_bootstrap_rejects_extra_inventory_variables(self) -> None:
        result = self.run_preflight(
            """---
all:
  children:
    spiritvpn_platform_bootstrap:
      hosts:
        management-1:
          ansible_host: 1.1.1.1
          ansible_user: root
          vault_token: forbidden
""",
            "1.1.1.1 ssh-ed25519 Zml4dHVyZS1wdWJsaWMta2V5\n",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("only ansible_host and ansible_user", result.stderr)

    def test_github_forced_command_has_environment_bound_operations(self) -> None:
        gate = REPO_ROOT / "roles" / "platform_executor" / "templates" / "spiritvpn-github-command.j2"
        subprocess.run(["bash", "-n", str(gate)], check=True)
        text = gate.read_text(encoding="utf-8")
        self.assertIn("original_command\" == platform-readiness", text)
        self.assertIn("fleet-deploy", text)
        self.assertIn("platform-deploy", text)
        self.assertIn("control-deploy", text)
        self.assertIn("cross-environment deployment", text)
        self.assertIn("bound_environment", text)
        self.assertNotIn("eval", text)
        self.assertNotIn("fleetctl deploy", text)
        self.assertNotIn("ansible-playbook", text)
        authorized_keys = (
            REPO_ROOT / "roles" / "platform_executor" / "templates" / "authorized_keys.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("spiritvpn-github-command {{ key.environment }}", authorized_keys)

    def test_remote_client_is_pinned_noninteractive_and_allowlisted(self) -> None:
        path = REPO_ROOT / "scripts" / "platform-remote.sh"
        subprocess.run(["bash", "-n", str(path)], check=True)
        text = path.read_text(encoding="utf-8")
        for required in ("StrictHostKeyChecking=yes", "BatchMode=yes", "IdentitiesOnly=yes"):
            self.assertIn(required, text)
        self.assertNotIn("ssh-keyscan", text)

    def test_operator_bootstrap_entrypoint_keeps_vault_ceremony_manual(self) -> None:
        path = REPO_ROOT / "scripts" / "platform-bootstrap.sh"
        subprocess.run(["bash", "-n", str(path)], check=True)
        text = path.read_text(encoding="utf-8")
        for required in (
            "git diff --quiet",
            "make check",
            "make lint",
            "fleet-platform-bootstrap-check",
            "fleet-platform-bootstrap",
            "sudo wg show spiritvpn-mgmt",
        ):
            self.assertIn(required, text)
        self.assertIn('[[ "$confirmation" == APPLY ]]', text)
        self.assertNotIn("vault operator init", text)
        self.assertNotIn("spiritvpn-vault-operator init", text)

    def test_vault_is_loopback_only_and_generates_tls_on_host(self) -> None:
        compose = (REPO_ROOT / "roles" / "platform_vault" / "templates" / "compose.yml.j2").read_text(
            encoding="utf-8"
        )
        tasks = (REPO_ROOT / "roles" / "platform_vault" / "tasks" / "main.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('"127.0.0.1:{{ platform_vault_api_port }}', compose)
        self.assertIn("- genpkey", tasks)
        self.assertNotIn("platform_vault_tls_private_key", tasks)
        self.assertNotIn("operator init", tasks)
        self.assertNotIn("operator unseal", tasks)
        defaults = (REPO_ROOT / "roles" / "platform_vault" / "defaults" / "main.yml").read_text(
            encoding="utf-8"
        )
        self.assertRegex(
            defaults,
            r"hashicorp/vault:1\.21\.4@sha256:[0-9a-f]{64}",
        )

    def test_platform_bootstrap_creates_wireguard_without_laptop_cidr(self) -> None:
        playbook = (REPO_ROOT / "playbooks" / "platform" / "bootstrap.yml").read_text(
            encoding="utf-8"
        )
        wireguard = (REPO_ROOT / "roles" / "platform_wireguard" / "tasks" / "main.yml").read_text(
            encoding="utf-8"
        )
        node_wireguard = (
            REPO_ROOT / "roles" / "bootstrap_wireguard" / "tasks" / "main.yml"
        ).read_text(encoding="utf-8")
        firewall = (REPO_ROOT / "roles" / "common" / "templates" / "nftables.conf.j2").read_text(
            encoding="utf-8"
        )
        self.assertIn("role: platform_wireguard", playbook)
        self.assertIn("Generate the management WireGuard private key locally once", wireguard)
        self.assertIn("creates: \"{{ platform_wireguard_private_key_path }}\"", wireguard)
        self.assertIn("Reconcile this node as a management-hub peer", node_wireguard)
        self.assertIn("delegate_to: localhost", node_wireguard)
        self.assertNotIn("platform_ssh_allowed_cidrs | join(' ')", playbook)
        self.assertIn("common_trusted_interfaces", firewall)
        self.assertIn("table inet spiritvpn_filter", firewall)

    def test_two_phase_platform_script_verifies_tunnel_before_hardening(self) -> None:
        script = (REPO_ROOT / "scripts" / "bootstrap-platform.py").read_text(
            encoding="utf-8"
        )
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        for required in (
            "playbooks/platform/wireguard-bootstrap.yml",
            "verifying pinned SSH through WireGuard",
            "playbooks/platform/bootstrap.yml",
            "refusing to overwrite unmanaged WireGuard config",
            "--verify-convergence",
        ):
            self.assertIn(required, script)
        self.assertLess(
            script.index("verifying pinned SSH through WireGuard"),
            script.index("phase 2/2: applying hardening"),
        )
        self.assertIn("scripts/bootstrap-platform.py --apply --verify-convergence", makefile)

        result = subprocess.run(
            [
                "python3",
                str(REPO_ROOT / "scripts" / "bootstrap-platform.py"),
                "--bundle",
                str(REPO_ROOT / "inventories" / "bootstrap" / "platform.sops.yml"),
                "--operator-wireguard-private-key",
                "/does/not/matter/without-apply",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("refusing mutation without --apply", result.stderr)

    def test_platform_executor_installs_a_compatible_python(self) -> None:
        defaults = (
            REPO_ROOT / "roles" / "platform_executor" / "defaults" / "main.yml"
        ).read_text(encoding="utf-8")
        tasks = (
            REPO_ROOT / "roles" / "platform_executor" / "tasks" / "main.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("platform_executor_python_minimum_minor: 11", defaults)
        self.assertIn("platform_executor_python_executable: /usr/bin/python3.11", defaults)
        self.assertIn("python3.11-venv", defaults)
        self.assertIn("platform_executor_python_executable", tasks)
        self.assertIn("Remove an incompatible executor Python environment", tasks)

    def test_github_workflow_cannot_mutate_or_receive_vault_credentials(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "platform-readiness.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("platform-remote.sh", workflow)
        self.assertIn("PLATFORM_SSH_PRIVATE_KEY", workflow)
        self.assertIn("secrets.PLATFORM_SSH_HOST", workflow)
        self.assertIn("secrets.PLATFORM_SSH_KNOWN_HOSTS", workflow)
        self.assertIn("runs-on: [self-hosted, linux, spiritvpn-deploy]", workflow)
        self.assertNotIn("vars.PLATFORM_SSH_HOST", workflow)
        self.assertNotIn("inventories/bootstrap/known_hosts", workflow)
        for forbidden in ("VAULT_TOKEN", "id-token: write", "fleet-deploy", "ansible-playbook", "ssh-keyscan"):
            self.assertNotIn(forbidden, workflow)

    def test_deploy_workflow_uses_reviewed_sha_bundle_and_no_vault_credential(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "fleet-deploy.yml").read_text(
            encoding="utf-8"
        )
        for required in (
            "workflow_dispatch",
            "git merge-base --is-ancestor",
            "git bundle create",
            "platform-remote.sh",
            # See test_control_deploy_is_digest_pinned_local_and_environment_bound:
            # the GitHub `environment:` key is unavailable on the free plan for
            # private repositories, so the binding is asserted at run time here
            # and by the forced command on the github-deploy key.
            '[[ "$REQUESTED_ENVIRONMENT" =~ ^(develop|prod)$ ]]',
            "secrets.PLATFORM_SSH_HOST",
            "secrets.PLATFORM_SSH_KNOWN_HOSTS",
            "runs-on: [self-hosted, linux, spiritvpn-deploy]",
        ):
            self.assertIn(required, workflow)
        self.assertNotIn("vars.PLATFORM_SSH_HOST", workflow)
        self.assertNotIn("inventories/bootstrap/known_hosts", workflow)
        for forbidden in ("VAULT_TOKEN", "id-token: write", "ssh-keyscan", "update-deployment-ref"):
            self.assertNotIn(forbidden, workflow)

    def test_platform_deploy_uses_exact_sha_and_local_protected_config(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "platform-deploy.yml").read_text(
            encoding="utf-8"
        )
        executor = (
            REPO_ROOT
            / "roles"
            / "platform_executor"
            / "templates"
            / "spiritvpn-platform-deploy.j2"
        ).read_text(encoding="utf-8")
        for required in (
            "git merge-base --is-ancestor",
            "git bundle create",
            "platform-remote.sh",
            # See test_control_deploy_is_digest_pinned_local_and_environment_bound:
            # the GitHub `environment:` key is unavailable on the free plan for
            # private repositories, so the binding is asserted at run time here
            # and by the forced command on the github-deploy key.
            '[[ "$REQUESTED_ENVIRONMENT" =~ ^(develop|prod)$ ]]',
            "runs-on: [self-hosted, linux, spiritvpn-deploy]",
        ):
            self.assertIn(required, workflow)
        for forbidden in ("VAULT_TOKEN", "SOPS_AGE_KEY", "ssh-keyscan"):
            self.assertNotIn(forbidden, workflow)
        for required in (
            'bundle verify "$bundle"',
            "refs/spiritvpn/platform-source",
            "playbooks/platform/steady.yml",
            "platform_runtime_vars_file",
        ):
            self.assertIn(required, executor)
        self.assertNotIn("eval", executor)

    def test_control_deploy_is_digest_pinned_local_and_environment_bound(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "control-deploy.yml").read_text(
            encoding="utf-8"
        )
        executor = (
            REPO_ROOT
            / "roles"
            / "platform_executor"
            / "templates"
            / "spiritvpn-control-deploy.j2"
        ).read_text(encoding="utf-8")
        compose = (
            REPO_ROOT / "roles" / "control_runtime" / "templates" / "compose.yml.j2"
        ).read_text(encoding="utf-8")
        tasks = (REPO_ROOT / "roles" / "control_runtime" / "tasks" / "main.yml").read_text(
            encoding="utf-8"
        )
        for required in (
            "git merge-base --is-ancestor",
            "refs/spiritvpn/control-source",
            "platform-remote.sh",
            # The environment binding is asserted at run time, not by a GitHub
            # `environment:` key. That key is unavailable on the free plan for
            # private repositories and made the job fail before its first step,
            # so it was removed; the binding it was standing in for lives here
            # and in the forced command attached to the github-deploy key.
            '[[ "$REQUESTED_ENVIRONMENT" =~ ^(develop|prod)$ ]]',
        ):
            self.assertIn(required, workflow)
        for forbidden in ("VAULT_TOKEN", "SOPS_AGE_KEY", "ssh-keyscan"):
            self.assertNotIn(forbidden, workflow)
        for required in (
            'bundle verify "$bundle"',
            "--scope control",
            "control-plan.json",
            "playbooks/control/deploy.yml",
            "refs/control-deployments/$environment",
        ):
            self.assertIn(required, executor)
        self.assertIn("control_plan.backend.image", compose)
        self.assertIn("control_plan.backend.migration_image", compose)
        self.assertIn("control_plan.postgres.image", compose)
        self.assertIn("Refuse an implicit PostgreSQL major-version upgrade", tasks)
        self.assertNotIn("--force-recreate", tasks)
        self.assertNotIn("compose\n      - down", tasks)

    def test_metrics_surfaces_never_leave_the_management_overlay(self) -> None:
        control_compose = (
            REPO_ROOT / "roles" / "control_runtime" / "templates" / "compose.yml.j2"
        ).read_text(encoding="utf-8")
        collector = REPO_ROOT / "roles" / "platform_observability"
        collector_tasks = (collector / "tasks" / "main.yml").read_text(encoding="utf-8")
        collector_compose = (
            collector / "templates" / "compose.yml.j2"
        ).read_text(encoding="utf-8")
        node_plan = (
            REPO_ROOT / "roles" / "compiled_node_plan" / "tasks" / "main.yml"
        ).read_text(encoding="utf-8")

        # The backend service port carries /metrics without TLS or caller
        # checks; publishing it on a wildcard would expose the whole fleet.
        self.assertIn(
            "{{ control_plan.network.management_address }}:"
            "{{ control_plan.network.backend_metrics_port }}:"
            "{{ control_plan.network.backend_metrics_port }}",
            control_compose,
        )
        self.assertNotIn("0.0.0.0", control_compose)

        # Scraping is outbound. The collector sits on a host that also holds
        # Vault, so it gets no inbound listener beyond loopback at all.
        self.assertIn(
            "--web.listen-address={{ platform_prometheus_bind_address }}",
            collector_compose,
        )
        self.assertIn("platform_prometheus_bind_address == '127.0.0.1'", collector_tasks)
        self.assertNotIn("--web.enable-admin-api", collector_compose)
        self.assertIn("--web.enable-lifecycle=false", collector_compose)
        self.assertIn("promtool", collector_tasks)

        # Node-side listeners bind the overlay and the firewall repeats it.
        self.assertIn(
            "node_exporter_bind_address: "
            '"{{ spiritvpn_node_plan.instance.management_address }}"',
            node_plan,
        )
        self.assertIn("observability.ports.agent_metrics", node_plan)

    def test_one_collector_is_shared_but_each_environment_writes_only_its_own(self) -> None:
        collector = REPO_ROOT / "roles" / "platform_observability"
        skeleton = (collector / "templates" / "prometheus.yml.j2").read_text(encoding="utf-8")
        collector_tasks = (collector / "tasks" / "main.yml").read_text(encoding="utf-8")
        control_tasks = (
            REPO_ROOT / "roles" / "control_observability" / "tasks" / "main.yml"
        ).read_text(encoding="utf-8")
        steady = (REPO_ROOT / "playbooks" / "platform" / "steady.yml").read_text(
            encoding="utf-8"
        )
        executor = (
            REPO_ROOT
            / "roles"
            / "platform_executor"
            / "templates"
            / "spiritvpn-control-deploy.j2"
        ).read_text(encoding="utf-8")

        # The shared skeleton belongs to the platform contour, beside Vault.
        self.assertIn("role: platform_observability", steady)
        self.assertIn("file_sd_configs", skeleton)
        self.assertIn("platform_observability_environments", skeleton)

        # The environment-bound deployment writes fragments and nothing else:
        # no compose, no collector process, no shared configuration file.
        self.assertNotIn("docker", control_tasks)
        self.assertNotIn("compose", control_tasks)
        self.assertIn("fragment.json.j2", control_tasks)
        self.assertIn(
            "{{ collector_contract.targets_dir }}/{{ control_plan.environment }}/",
            control_tasks,
        )
        # Fragments written before the collector exists would be read by nobody.
        self.assertIn("Refuse to write fragments no collector will read", control_tasks)
        # A shared TSDB has one cadence; desired state must not claim another.
        self.assertIn(
            "collector_contract.scrape_interval_seconds | int",
            control_tasks,
        )
        # Only management-collected metrics are scraped: node-local expvar is
        # unreachable from here and external probes have no exporter yet.
        self.assertIn("selectattr('collection', 'equalto', 'management')", control_tasks)
        self.assertIn("monitoring_targets.environment == control_plan.environment", control_tasks)
        self.assertIn("monitoring-targets.json", executor)

        # A platform run must not erase what an environment wrote.
        self.assertIn("force: false", collector_tasks)

    def test_skeleton_reads_exactly_where_the_environments_write(self) -> None:
        """The two roles meet only at a path, and nothing else checks it.

        The skeleton names container paths, the fragments are written to host
        paths, and a bind mount joins them. Any of the three can be edited on
        its own, and the failure would be an empty graph rather than an error.
        """
        collector = REPO_ROOT / "roles" / "platform_observability"
        defaults = yaml.safe_load(
            (collector / "defaults" / "main.yml").read_text(encoding="utf-8")
        )
        jinja = Environment(  # noqa: S701 - rendering our own template for inspection
            keep_trailing_newline=True, trim_blocks=True, lstrip_blocks=False
        )
        jinja.filters["comment"] = lambda value: str(value)
        skeleton_source = (
            collector / "templates" / "prometheus.yml.j2"
        ).read_text(encoding="utf-8")
        skeleton = yaml.safe_load(
            jinja.from_string(skeleton_source).render(ansible_managed="", **defaults)
        )

        declared = {
            path
            for job in skeleton["scrape_configs"]
            for config in job.get("file_sd_configs", [])
            for path in config["files"]
        }
        host_directory = defaults["platform_observability_targets_dir"]
        container_directory = defaults["platform_prometheus_targets_container_dir"]

        # The compose definition is what makes the two path spaces the same
        # directory; without this mount the skeleton would read an empty path.
        compose = yaml.safe_load(
            jinja.from_string(
                (collector / "templates" / "compose.yml.j2").read_text(encoding="utf-8")
            ).render(**defaults)
        )
        self.assertIn(
            f"{host_directory}:{container_directory}:ro",
            compose["services"]["prometheus"]["volumes"],
        )

        expected = {
            f"{container_directory}/{environment}/{job['name']}.json"
            for environment in defaults["platform_observability_environments"]
            for job in defaults["platform_observability_jobs"]
        }
        self.assertEqual(declared, expected)
        # Every environment in the schema gets jobs, or its metrics silently
        # go nowhere.
        self.assertEqual(
            sorted(defaults["platform_observability_environments"]), ["develop", "prod"]
        )

    def test_root_executor_is_fail_closed_and_does_not_move_deployment_ref(self) -> None:
        path = REPO_ROOT / "roles" / "platform_executor" / "templates" / "spiritvpn-fleet-deploy.j2"
        text = path.read_text(encoding="utf-8")
        for required in (
            'bundle verify "$bundle"',
            "refs/spiritvpn/source",
            "vault-secret-resolver.py",
            "StrictHostKeyChecking=yes",
            "--state-dir",
        ):
            self.assertIn(required, text)
        self.assertNotIn("update-deployment-ref", text)
        self.assertNotIn("eval", text)

    def run_resolver_against_fake_vault(
        self, workspace: Path, *, fail_reads: bool
    ) -> subprocess.CompletedProcess[str]:
        RecordingVault.calls = []
        RecordingVault.fail_reads = fail_reads
        server = HTTPServer(("127.0.0.1", 0), RecordingVault)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            # The address is plain HTTP so the test needs no TLS, but the
            # resolver still builds an SSL context, so --vault-ca must parse.
            authority = workspace / "ca.crt"
            subprocess.run(
                [
                    "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(workspace / "ca.key"), "-out", str(authority),
                    "-days", "1", "-subj", "/CN=test",
                ],
                check=True,
                capture_output=True,
            )
            credentials = workspace / "approle"
            credentials.mkdir()
            (credentials / "role-id").write_text("role", encoding="utf-8")
            (credentials / "secret-id").write_text("secret", encoding="utf-8")
            return subprocess.run(
                [
                    "python3",
                    str(REPO_ROOT / "scripts" / "vault-secret-resolver.py"),
                    "--root", str(REPO_ROOT),
                    "--desired-root", str(VALID_DESIRED),
                    "--environment", "develop",
                    "--scope", "fleet",
                    "--credentials-dir", str(credentials),
                    "--compiled-secrets", str(workspace / "compiled.yml"),
                    "--vault-address", f"http://127.0.0.1:{server.server_address[1]}",
                    "--vault-ca", str(authority),
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
        finally:
            server.shutdown()
            server.server_close()

    def test_vault_token_is_revoked_on_success_and_on_failure(self) -> None:
        """A surviving AppRole token is a usable credential left on the executor.

        It outlives the process for its whole TTL otherwise, and the failure
        path is exactly where that matters most.
        """
        for fail_reads, expected_status in ((False, 0), (True, 2)):
            with self.subTest(fail_reads=fail_reads):
                with tempfile.TemporaryDirectory() as temporary:
                    workspace = Path(temporary)
                    result = self.run_resolver_against_fake_vault(
                        workspace, fail_reads=fail_reads
                    )
                    self.assertEqual(result.returncode, expected_status, result.stderr)
                    revocations = [
                        call
                        for call in RecordingVault.calls
                        if call[1] == "/v1/auth/token/revoke-self"
                    ]
                    self.assertEqual(len(revocations), 1, RecordingVault.calls)
                    self.assertEqual(revocations[0][2], "s.TEST")
                    self.assertEqual(revocations[0][0], "POST")
                    if not fail_reads:
                        compiled = workspace / "compiled.yml"
                        self.assertEqual(compiled.stat().st_mode & 0o777, 0o600)

    def test_vault_operator_is_manual_and_environment_scoped(self) -> None:
        operator = (
            REPO_ROOT / "roles" / "platform_vault" / "templates" / "spiritvpn-vault-operator.j2"
        ).read_text(encoding="utf-8")
        policy = (
            REPO_ROOT / "roles" / "platform_vault" / "templates" / "policy-fleet-deployer.hcl.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("operator init", operator)
        self.assertIn("operator unseal", operator)
        self.assertIn("secret_id_bound_cidrs=127.0.0.1/32", operator)
        self.assertIn("token_no_default_policy=true", operator)
        self.assertNotIn('-e "VAULT_TOKEN=$root_token"', operator)
        self.assertNotIn("| grep -q", operator)
        self.assertIn('auth_json="$(vault_as_root auth list -format=json)"', operator)
        self.assertIn('[[ "$auth_json" != *\'"approle/"\'* ]]', operator)
        self.assertIn('path "kv/data/{{ policy_environment }}/*"', policy)
        self.assertNotIn('capabilities = ["create"', policy)

    def test_secret_reference_listing_is_offline_and_environment_scoped(self) -> None:
        result = subprocess.run(
            [
                "python3",
                str(REPO_ROOT / "scripts" / "vault-secret-resolver.py"),
                "--root",
                str(REPO_ROOT),
                "--desired-root",
                str(REPO_ROOT / "tests" / "fixtures" / "valid" / "desired"),
                "--environment",
                "develop",
                "--list-references",
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        references = result.stdout.splitlines()
        self.assertTrue(references)
        self.assertEqual(references, sorted(references[:-1]) + [references[-1]])
        self.assertTrue(all(reference.startswith("secret://kv/develop/") for reference in references))
        self.assertEqual(
            references[-1],
            "secret://kv/develop/executor/ansible#private_key",
        )

    def test_control_secret_scope_excludes_fleet_and_executor_credentials(self) -> None:
        result = subprocess.run(
            [
                "python3",
                str(REPO_ROOT / "scripts" / "vault-secret-resolver.py"),
                "--root",
                str(REPO_ROOT),
                "--desired-root",
                str(REPO_ROOT / "tests" / "fixtures" / "valid" / "desired"),
                "--environment",
                "develop",
                "--scope",
                "control",
                "--list-references",
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        references = result.stdout.splitlines()
        self.assertEqual(len(references), 11)
        self.assertTrue(
            all(reference.startswith("secret://kv/develop/control/") for reference in references)
        )
        self.assertFalse(any("executor/ansible" in reference for reference in references))

    def test_private_writer_refuses_symlink_and_sets_mode(self) -> None:
        script = REPO_ROOT / "scripts" / "vault-secret-resolver.py"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            link = root / "link"
            target.write_text("unchanged", encoding="utf-8")
            link.symlink_to(target)
            command = (
                "import importlib.util,pathlib;"
                f"s=importlib.util.spec_from_file_location('resolver',{str(script)!r});"
                "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
                f"p=pathlib.Path({str(link)!r});"
                "m.write_private(p,'secret')"
            )
            result = subprocess.run(
                ["python3", "-c", command], text=True, capture_output=True, check=False
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(target.read_text(encoding="utf-8"), "unchanged")

            output = root / "output"
            command = command.replace(str(link), str(output))
            result = subprocess.run(["python3", "-c", command], check=False)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)

    def test_executor_templates_have_valid_shell_after_jinja_substitution(self) -> None:
        paths = (
            REPO_ROOT / "roles" / "platform_executor" / "templates" / "spiritvpn-platform-readiness.j2",
            REPO_ROOT / "roles" / "platform_executor" / "templates" / "spiritvpn-fleet-deploy.j2",
            REPO_ROOT / "roles" / "platform_executor" / "templates" / "spiritvpn-platform-deploy.j2",
            REPO_ROOT / "roles" / "platform_executor" / "templates" / "spiritvpn-control-deploy.j2",
            REPO_ROOT / "roles" / "platform_vault" / "templates" / "spiritvpn-vault-operator.j2",
            REPO_ROOT / "roles" / "platform_wireguard" / "templates" / "spiritvpn-wireguard-reconcile.j2",
            REPO_ROOT / "roles" / "platform_wireguard" / "templates" / "spiritvpn-wireguard-peer.j2",
        )
        for path in paths:
            source = path.read_text(encoding="utf-8")
            Environment().parse(source)
            rendered = re.sub(r"{{[^\n{}]+}}", "fixture", source)
            self.assertNotIn("{{", rendered)
            subprocess.run(["bash", "-n"], input=rendered, text=True, check=True)


if __name__ == "__main__":
    unittest.main()
