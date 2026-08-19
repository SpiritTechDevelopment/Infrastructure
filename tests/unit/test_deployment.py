from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from fleetctl.deployment import DeploymentCoordinator, DeploymentError, DeploymentOptions
from tests.unit.test_git_adapter import TemporaryFleetRepository


class InfrastructureDeploymentCoordinatorTests(unittest.TestCase):
    def fake_signer(self, coordinator: DeploymentCoordinator):
        """Stand in for the offline CA.

        Signing is covered by the PKI tests; here the only thing that matters is
        that the coordinator collects, signs and hands the chains to the install
        phase in the right order.
        """

        def sign(*, targets, current, ca_state, pki_directory):
            del current, ca_state, pki_directory
            return {instance: f"-----BEGIN CERTIFICATE-----\n{instance}\n" for instance in targets}

        return mock.patch.object(coordinator, "_sign_agent_certificates", side_effect=sign)

    def fake_backend(self):
        """Stand in for the backend that now hears from every apply.

        The coordinator issues its own manifest-writer identity, so an apply no
        longer stops at the contract boundary on its own. Tests about Ansible
        targeting and resume should not need a gRPC stack or a live backend to
        say what they are about.
        """

        return mock.patch(
            "fleetctl.deployment.coordinator.apply_fleet_manifest",
            return_value="MANIFEST_APPLY_RESULT_APPLIED",
        )

    def prepare_repository(self, parent: Path) -> TemporaryFleetRepository:
        root = parent / "repository"
        root.mkdir()
        repository = TemporaryFleetRepository(root)
        instances = repository.root / "desired" / "environments" / "develop" / "instances"
        for name, address in (
            ("develop-entry-nl-01.yml", "1.1.1.1"),
            ("develop-exit-de-01.yml", "8.8.8.8"),
        ):
            path = instances / name
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            document["spec"]["public_address"] = address
            path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        repository.git("add", "desired")
        repository.git("commit", "-qm", "use routable fixture addresses")
        return repository

    def test_dry_run_reaches_waiting_for_backend_without_external_commands_or_ref_update(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.prepare_repository(Path(temporary))
            source = repository.head()
            coordinator = DeploymentCoordinator(repository.root)
            with mock.patch.object(coordinator, "_run_ansible", side_effect=AssertionError("unexpected Ansible")):
                record = coordinator.run(DeploymentOptions(environment="develop", initial=True))

            ref = repository.git("for-each-ref", "--format=%(refname)", "refs/deployments/develop")
            statuses = {step["name"]: step["status"] for step in record["steps"]}
            manifest_path = repository.root / "build" / "develop" / "backend-manifest.json"
            manifest_bytes = manifest_path.read_bytes()
            revision_path = (
                repository.root
                / ".fleetctl-state"
                / "manifest-revisions"
                / "develop.json"
            )
            revision_state = json.loads(revision_path.read_text(encoding="utf-8"))
            revision_mode = oct(revision_path.stat().st_mode & 0o777)

        self.assertEqual(record["status"], "WAITING_FOR_BACKEND")
        self.assertTrue(record["dry_run"])
        self.assertFalse(record["deployment_ref_updated"])
        self.assertEqual(record["source_git_sha"], source)
        self.assertEqual(ref, "")
        self.assertEqual(statuses["bootstrap_csr"], "SKIPPED_DRY_RUN")
        self.assertEqual(statuses["sign_agent_certificates"], "SKIPPED_DRY_RUN")
        self.assertEqual(statuses["bootstrap"], "SKIPPED_DRY_RUN")
        self.assertEqual(statuses["configure"], "SKIPPED_DRY_RUN")
        self.assertEqual(statuses["readiness_gates"], "SKIPPED_DRY_RUN")
        self.assertEqual(statuses["allocate_backend_manifest_revision"], "COMPLETED")
        self.assertEqual(record["backend_manifest"]["revision"], 1)
        self.assertEqual(
            record["backend_manifest"]["rendered_sha256"],
            f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}",
        )
        self.assertEqual(record["backend_manifest"]["size_bytes"], len(manifest_bytes))
        self.assertEqual(record["backend_apply"]["status"], "NOT_SENT")
        self.assertEqual(revision_state["last_allocated_revision"], 1)
        self.assertEqual(revision_mode, "0o600")

    def test_resume_is_explicit_and_preserves_completed_steps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.prepare_repository(Path(temporary))
            coordinator = DeploymentCoordinator(repository.root)
            options = DeploymentOptions(environment="develop", initial=True)
            first = coordinator.run(options)
            with self.assertRaises(DeploymentError):
                coordinator.run(options)
            resumed = coordinator.run(
                DeploymentOptions(environment="develop", initial=True, resume=True)
            )
            revision_path = (
                repository.root
                / ".fleetctl-state"
                / "manifest-revisions"
                / "develop.json"
            )
            revision_state = json.loads(revision_path.read_text(encoding="utf-8"))

        self.assertEqual(resumed["deployment_id"], first["deployment_id"])
        self.assertEqual(resumed["status"], "WAITING_FOR_BACKEND")
        self.assertEqual(resumed["backend_manifest"], first["backend_manifest"])
        self.assertEqual(revision_state["last_allocated_revision"], 1)
        self.assertEqual(len(revision_state["allocations"]), 1)
        names = [step["name"] for step in resumed["steps"]]
        self.assertEqual(len(names), len(set(names)))

    def test_new_deployment_gets_next_revision_in_same_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.prepare_repository(Path(temporary))
            coordinator = DeploymentCoordinator(repository.root)
            first = coordinator.run(DeploymentOptions(environment="develop", initial=True))
            node_path = (
                repository.root
                / "desired"
                / "environments"
                / "develop"
                / "nodes"
                / "develop-entry-nl.yml"
            )
            node = yaml.safe_load(node_path.read_text(encoding="utf-8"))
            node["spec"]["display_name"] = "Netherlands updated"
            node_path.write_text(yaml.safe_dump(node, sort_keys=False), encoding="utf-8")
            repository.git("add", "desired")
            repository.git("commit", "-qm", "change manifest payload")
            second = coordinator.run(DeploymentOptions(environment="develop", initial=True))

        self.assertEqual(first["backend_manifest"]["revision"], 1)
        self.assertEqual(second["backend_manifest"]["revision"], 2)
        self.assertNotEqual(
            first["backend_manifest"]["payload_digest"],
            second["backend_manifest"]["payload_digest"],
        )

    def test_destructive_manifest_requires_and_records_explicit_permission(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.prepare_repository(Path(temporary))
            baseline = repository.head()
            repository.git("update-ref", "refs/deployments/develop", baseline)
            fleet_path = (
                repository.root
                / "desired"
                / "environments"
                / "develop"
                / "fleets"
                / "develop-fleet-eu.yml"
            )
            fleet = yaml.safe_load(fleet_path.read_text(encoding="utf-8"))
            fleet["spec"]["entries"] = []
            fleet["spec"]["bridges"] = []
            fleet_path.write_text(yaml.safe_dump(fleet, sort_keys=False), encoding="utf-8")
            repository.git("add", "desired")
            repository.git("commit", "-qm", "begin two-phase node decommission")

            record = DeploymentCoordinator(repository.root).run(
                DeploymentOptions(environment="develop", allow_destructive=True)
            )

        self.assertTrue(record["backend_manifest"]["allow_destructive"])
        self.assertEqual(record["backend_manifest"]["revision"], 1)
        self.assertEqual(record["backend_apply"]["status"], "NOT_SENT")

    def test_resume_fails_closed_when_revision_state_is_lost(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.prepare_repository(Path(temporary))
            coordinator = DeploymentCoordinator(repository.root)
            coordinator.run(DeploymentOptions(environment="develop", initial=True))
            revision_path = (
                repository.root
                / ".fleetctl-state"
                / "manifest-revisions"
                / "develop.json"
            )
            revision_path.unlink()

            with self.assertRaisesRegex(DeploymentError, "revision state is missing"):
                coordinator.run(
                    DeploymentOptions(environment="develop", initial=True, resume=True)
                )

    def test_resume_fails_closed_when_pinned_manifest_metadata_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.prepare_repository(Path(temporary))
            coordinator = DeploymentCoordinator(repository.root)
            record = coordinator.run(DeploymentOptions(environment="develop", initial=True))
            record_path = (
                repository.root
                / ".fleetctl-state"
                / "deployment-records"
                / f"{record['deployment_id']}.json"
            )
            stored = json.loads(record_path.read_text(encoding="utf-8"))
            stored["backend_manifest"]["rendered_sha256"] = "sha256:" + "0" * 64
            record_path.write_text(json.dumps(stored), encoding="utf-8")
            os.chmod(record_path, 0o600)

            with self.assertRaisesRegex(DeploymentError, "differs from the revision pinned"):
                coordinator.run(
                    DeploymentOptions(environment="develop", initial=True, resume=True)
                )

    def test_explicit_apply_resume_cannot_keep_a_false_dry_run_label(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.prepare_repository(Path(temporary))
            coordinator = DeploymentCoordinator(repository.root)
            coordinator.run(DeploymentOptions(environment="develop", initial=True))
            variables = {}
            for name in ("bootstrap.yml", "secrets.yml", "readiness.yml"):
                path = repository.root / name
                path.write_text("{}\n", encoding="utf-8")
                variables[name] = path
            with mock.patch.object(coordinator, "_run_ansible"), self.fake_signer(
                coordinator
            ), self.fake_backend():
                resumed = coordinator.run(
                    DeploymentOptions(
                        environment="develop",
                        initial=True,
                        resume=True,
                        apply=True,
                        bootstrap_vars=variables["bootstrap.yml"],
                        compiled_secrets=variables["secrets.yml"],
                        readiness_vars=variables["readiness.yml"],
                        ca_state=repository.root / "ca",
                    )
                )

        self.assertFalse(resumed["dry_run"])
        # An apply now issues its own manifest-writer identity, so it no longer
        # stops at the contract boundary for want of one.
        self.assertEqual(resumed["status"], "BACKEND_APPLIED")

    def test_apply_requires_all_operator_inputs_before_ansible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.prepare_repository(Path(temporary))
            coordinator = DeploymentCoordinator(repository.root)
            with mock.patch.object(coordinator, "_run_ansible") as ansible:
                with self.assertRaises(DeploymentError):
                    coordinator.run(
                        DeploymentOptions(environment="develop", initial=True, apply=True)
                    )
                ansible.assert_not_called()

    def test_apply_limits_ansible_to_impact_plan_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.prepare_repository(Path(temporary))
            coordinator = DeploymentCoordinator(repository.root)
            variables = {}
            for name in ("bootstrap.yml", "secrets.yml", "readiness.yml"):
                path = repository.root / name
                path.write_text("{}\n", encoding="utf-8")
                variables[name] = path
            with mock.patch.object(coordinator, "_run_ansible") as ansible, self.fake_signer(
                coordinator
            ), self.fake_backend():
                coordinator.run(
                    DeploymentOptions(
                        environment="develop",
                        initial=True,
                        apply=True,
                        bootstrap_vars=variables["bootstrap.yml"],
                        compiled_secrets=variables["secrets.yml"],
                        readiness_vars=variables["readiness.yml"],
                        ca_state=repository.root / "ca",
                    )
                )

        # csr, bootstrap, configure, readiness — the CSR phase is its own run.
        self.assertEqual(ansible.call_count, 4)
        for call in ansible.call_args_list:
            self.assertEqual(
                call.kwargs["limit"],
                ("develop-entry-nl-01", "develop-exit-de-01"),
            )

    def test_already_bootstrapped_nodes_skip_bootstrap_but_still_get_configured(self) -> None:
        # The impact plan calls an instance "provision" whenever it is absent
        # from the Git baseline, which is every instance while no baseline
        # exists yet. The bootstrap inventory reaches nodes on port 22, and a
        # hardened node no longer answers there: without this marker a live node
        # drops out of the fleet precisely because the last deployment to it
        # succeeded. Configuration must still reach it.
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.prepare_repository(Path(temporary))
            coordinator = DeploymentCoordinator(repository.root)
            variables = {}
            for name in ("bootstrap.yml", "secrets.yml", "readiness.yml"):
                path = repository.root / name
                path.write_text("{}\n", encoding="utf-8")
                variables[name] = path
            markers = repository.root / ".fleetctl-state" / "bootstrapped" / "develop"
            markers.mkdir(parents=True)
            for instance in ("develop-entry-nl-01", "develop-exit-de-01"):
                (markers / f"{instance}.json").write_text("{}\n", encoding="utf-8")
            with mock.patch.object(coordinator, "_run_ansible") as ansible:
                # No ca_state and no signer: a fleet with nothing left to
                # bootstrap must not demand the offline CA root at all.
                coordinator.run(
                    DeploymentOptions(
                        environment="develop",
                        initial=True,
                        apply=True,
                        bootstrap_vars=variables["bootstrap.yml"],
                        compiled_secrets=variables["secrets.yml"],
                        readiness_vars=variables["readiness.yml"],
                    )
                )

        self.assertEqual(
            [call.args[1].name for call in ansible.call_args_list],
            ["configure.yml", "readiness.yml"],
        )
        for call in ansible.call_args_list:
            self.assertEqual(
                call.kwargs["limit"],
                ("develop-entry-nl-01", "develop-exit-de-01"),
            )

    def test_manifest_is_sent_once_and_a_resume_does_not_send_it_again(self) -> None:
        # The revision is durably allocated before anything is sent, so a resume
        # that re-sent it would be asking the backend to reconsider a decision it
        # has already made and recorded.
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.prepare_repository(Path(temporary))
            coordinator = DeploymentCoordinator(repository.root)
            variables = {}
            for name in ("bootstrap.yml", "secrets.yml", "readiness.yml"):
                path = repository.root / name
                path.write_text("{}\n", encoding="utf-8")
                variables[name] = path
            material = {}
            for name in ("tls.crt", "tls.key", "ca.crt"):
                path = repository.root / name
                path.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
                material[name] = path
            markers = repository.root / ".fleetctl-state" / "bootstrapped" / "develop"
            markers.mkdir(parents=True)
            for instance in ("develop-entry-nl-01", "develop-exit-de-01"):
                (markers / f"{instance}.json").write_text("{}\n", encoding="utf-8")

            options = DeploymentOptions(
                environment="develop",
                initial=True,
                apply=True,
                bootstrap_vars=variables["bootstrap.yml"],
                compiled_secrets=variables["secrets.yml"],
                readiness_vars=variables["readiness.yml"],
                backend_client_certificate=material["tls.crt"],
                backend_client_private_key=material["tls.key"],
                backend_certificate_authority=material["ca.crt"],
            )
            with mock.patch.object(coordinator, "_run_ansible"), mock.patch(
                "fleetctl.deployment.coordinator.apply_fleet_manifest",
                return_value="MANIFEST_APPLY_RESULT_APPLIED",
            ) as send:
                first = coordinator.run(options)
                resumed = coordinator.run(
                    dataclasses.replace(options, initial=True, resume=True)
                )

        self.assertEqual(send.call_count, 1)
        request = send.call_args.args[0]
        endpoint = send.call_args.kwargs["endpoint"]
        self.assertEqual(request["revision"], first["backend_manifest"]["revision"])
        # Reached over the management overlay, verified against the declared name.
        self.assertEqual(endpoint.target, "10.80.0.1:9443")
        self.assertEqual(endpoint.tls_server_name, "backend.develop.internal")

        self.assertEqual(first["status"], "BACKEND_APPLIED")
        self.assertEqual(
            first["backend_apply"],
            {
                "status": "APPLIED",
                "applied_revision": first["backend_manifest"]["revision"],
                "result": "MANIFEST_APPLY_RESULT_APPLIED",
            },
        )
        self.assertEqual(resumed["status"], "BACKEND_APPLIED")
        self.assertEqual(resumed["backend_apply"], first["backend_apply"])

    def test_bootstrapping_nodes_refuses_to_start_without_a_ca(self) -> None:
        # Fail before the CSR phase mutates anything: collecting requests that
        # nothing can sign would leave keys on nodes and no way forward.
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.prepare_repository(Path(temporary))
            coordinator = DeploymentCoordinator(repository.root)
            variables = {}
            for name in ("bootstrap.yml", "secrets.yml", "readiness.yml"):
                path = repository.root / name
                path.write_text("{}\n", encoding="utf-8")
                variables[name] = path
            with mock.patch.object(coordinator, "_run_ansible"):
                with self.assertRaisesRegex(DeploymentError, "requires --ca-state"):
                    coordinator.run(
                        DeploymentOptions(
                            environment="develop",
                            initial=True,
                            apply=True,
                            bootstrap_vars=variables["bootstrap.yml"],
                            compiled_secrets=variables["secrets.yml"],
                            readiness_vars=variables["readiness.yml"],
                        )
                    )

    def test_uncollected_csr_stops_the_deployment_before_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.prepare_repository(Path(temporary))
            coordinator = DeploymentCoordinator(repository.root)
            variables = {}
            for name in ("bootstrap.yml", "secrets.yml", "readiness.yml"):
                path = repository.root / name
                path.write_text("{}\n", encoding="utf-8")
                variables[name] = path
            playbooks: list[str] = []

            def record(_inventory, playbook, _variables, *, limit, **_keywords):
                del limit
                playbooks.append(playbook.name)

            # One node answered the CSR phase, the other did not.
            def partial_sign(*, targets, current, ca_state, pki_directory):
                del current, ca_state, pki_directory
                return {targets[0]: "-----BEGIN CERTIFICATE-----\n"}

            with mock.patch.object(
                coordinator, "_run_ansible", side_effect=record
            ), mock.patch.object(
                coordinator, "_sign_agent_certificates", side_effect=partial_sign
            ):
                with self.assertRaisesRegex(DeploymentError, "collected no request for"):
                    coordinator.run(
                        DeploymentOptions(
                            environment="develop",
                            initial=True,
                            apply=True,
                            bootstrap_vars=variables["bootstrap.yml"],
                            compiled_secrets=variables["secrets.yml"],
                            readiness_vars=variables["readiness.yml"],
                            ca_state=repository.root / "ca",
                        )
                    )

        self.assertEqual(playbooks, ["csr.yml"])

    def test_signed_chains_are_handed_to_the_install_phase_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.prepare_repository(Path(temporary))
            coordinator = DeploymentCoordinator(repository.root)
            variables = {}
            for name in ("bootstrap.yml", "secrets.yml", "readiness.yml"):
                path = repository.root / name
                path.write_text("{}\n", encoding="utf-8")
                variables[name] = path
            seen: dict[str, tuple[Path, ...]] = {}

            def record(_inventory, playbook, _variables, *, limit, extra_files=(), **_keywords):
                del limit
                seen[playbook.name] = extra_files

            with mock.patch.object(
                coordinator, "_run_ansible", side_effect=record
            ), self.fake_signer(coordinator), self.fake_backend():
                coordinator.run(
                    DeploymentOptions(
                        environment="develop",
                        initial=True,
                        apply=True,
                        bootstrap_vars=variables["bootstrap.yml"],
                        compiled_secrets=variables["secrets.yml"],
                        readiness_vars=variables["readiness.yml"],
                        ca_state=repository.root / "ca",
                    )
                )

            self.assertEqual(seen["csr.yml"], ())
            self.assertEqual(len(seen["bootstrap.yml"]), 1)
            chain_path = seen["bootstrap.yml"][0]
            chains = json.loads(chain_path.read_text(encoding="utf-8"))
            self.assertEqual(
                sorted(chains["spiritvpn_agent_certificate_chains"]),
                ["develop-entry-nl-01", "develop-exit-de-01"],
            )
            self.assertEqual(oct(chain_path.stat().st_mode & 0o777), "0o600")
            # The chains outlive the second render: the build directory is
            # replaced wholesale, so they cannot live there.
            self.assertNotIn("build", chain_path.parts)

    def test_no_desired_change_touches_no_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.prepare_repository(Path(temporary))
            baseline = repository.head()
            repository.git("update-ref", "refs/deployments/develop", baseline)
            documentation = repository.root / "README.md"
            documentation.write_text("documentation-only change\n", encoding="utf-8")
            repository.git("add", "README.md")
            repository.git("commit", "-qm", "documentation only")
            variables = {}
            for name in ("bootstrap.yml", "secrets.yml", "readiness.yml"):
                path = repository.root / name
                path.write_text("{}\n", encoding="utf-8")
                variables[name] = path
            coordinator = DeploymentCoordinator(repository.root)
            with mock.patch.object(coordinator, "_run_ansible") as ansible:
                record = coordinator.run(
                    DeploymentOptions(
                        environment="develop",
                        apply=True,
                        bootstrap_vars=variables["bootstrap.yml"],
                        compiled_secrets=variables["secrets.yml"],
                        readiness_vars=variables["readiness.yml"],
                    )
                )

        ansible.assert_not_called()
        node_steps = {
            step["name"]: step["diagnostic"]
            for step in record["steps"]
            if step["name"] in {"bootstrap_csr", "bootstrap", "configure", "readiness_gates"}
        }
        self.assertTrue(all("no affected nodes" in diagnostic for diagnostic in node_steps.values()))

    def test_resume_does_not_rerun_a_completed_bootstrap_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.prepare_repository(Path(temporary))
            variables = {}
            for name in ("bootstrap.yml", "secrets.yml", "readiness.yml"):
                path = repository.root / name
                path.write_text("{}\n", encoding="utf-8")
                variables[name] = path
            coordinator = DeploymentCoordinator(repository.root)
            options = DeploymentOptions(
                environment="develop",
                initial=True,
                apply=True,
                bootstrap_vars=variables["bootstrap.yml"],
                compiled_secrets=variables["secrets.yml"],
                readiness_vars=variables["readiness.yml"],
                ca_state=repository.root / "ca",
            )
            first_calls: list[str] = []

            def fail_configure(_inventory, playbook, _variables, *, limit, **_keywords):
                del limit
                first_calls.append(playbook.name)
                if playbook.name == "configure.yml":
                    raise DeploymentError("fixture configure failure")

            with mock.patch.object(
                coordinator, "_run_ansible", side_effect=fail_configure
            ), self.fake_signer(coordinator):
                with self.assertRaisesRegex(DeploymentError, "fixture configure failure"):
                    coordinator.run(options)

            resumed_calls: list[str] = []

            def record_call(_inventory, playbook, _variables, *, limit, **_keywords):
                del limit
                resumed_calls.append(playbook.name)

            with mock.patch.object(
                coordinator, "_run_ansible", side_effect=record_call
            ), self.fake_signer(coordinator) as resumed_signer, self.fake_backend():
                coordinator.run(
                    DeploymentOptions(
                        environment="develop",
                        initial=True,
                        resume=True,
                        apply=True,
                        bootstrap_vars=variables["bootstrap.yml"],
                        compiled_secrets=variables["secrets.yml"],
                        readiness_vars=variables["readiness.yml"],
                        ca_state=repository.root / "ca",
                    )
                )

        self.assertEqual(first_calls, ["csr.yml", "bootstrap.yml", "configure.yml"])
        self.assertEqual(resumed_calls, ["configure.yml", "readiness.yml"])
        # A completed signing step must not send the CSRs back to the CA.
        resumed_signer.assert_not_called()

    def test_environment_lock_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self.prepare_repository(Path(temporary))
            record_directory = repository.root / ".fleetctl-state" / "deployment-records"
            record_directory.mkdir(parents=True)
            lock_path = record_directory / "develop.lock"
            with lock_path.open("a+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(DeploymentError):
                    DeploymentCoordinator(repository.root).run(
                        DeploymentOptions(environment="develop", initial=True)
                    )


if __name__ == "__main__":
    unittest.main()
