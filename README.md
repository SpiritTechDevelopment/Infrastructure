# SpiritVPN infrastructure v1

This repository defines, validates, compiles, and coordinates the SpiritVPN
infrastructure desired state. The v1 contour is fail-closed: local validation and
rendering perform no network I/O, deployment is dry-run by default, and the
coordinator currently stops at the backend boundary.

Для последовательного объяснения системы простыми словами — включая Ansible,
Vault, PKI, GitHub workflow, первый запуск и текущие ограничения — см.
[`INFRA_V1_GUIDE_RU.md`](docs/operations/INFRA_V1_GUIDE_RU.md).

## Sources of truth

- [`docs/architecture/INFRA_TECHNICAL_SPEC.md`](docs/architecture/INFRA_TECHNICAL_SPEC.md)
  is the normative infrastructure specification.
- [`desired/`](desired/) is the human-edited, non-secret desired state.
- [`contracts/desired-state/`](contracts/desired-state/) defines its schemas.
- [`contracts/backend/BACKEND_DOMAIN_AGREEMENTS.md`](contracts/backend/BACKEND_DOMAIN_AGREEMENTS.md)
  is the vendored authority for backend-owned behavior.
- [`contracts/nodeagent/v1/node_agent.proto`](contracts/nodeagent/v1/node_agent.proto)
  is the backend-to-agent wire contract.
- [`docs/status/INFRA_V1_IMPLEMENTATION_STATUS.md`](docs/status/INFRA_V1_IMPLEMENTATION_STATUS.md)
  records implemented and missing capabilities.

Git describes desired state, protected secret storage grants access, and runtime
systems report observed health. Generated artifacts under `build/` are never a
source of truth.

## Safe local workflow

```bash
make fleet-validate
make fleet-test
make fleet-render ENVIRONMENT=develop
make fleet-ansible-check ENVIRONMENT=develop
make fleet-provisioning-check ENVIRONMENT=develop
```

Plan against the last successful deployment ref:

```bash
make fleet-plan ENVIRONMENT=develop SOURCE=HEAD
```

For the intentional first deployment only:

```bash
make fleet-plan ENVIRONMENT=develop SOURCE=HEAD INITIAL=1
```

The coordinator is also safe by default:

```bash
make fleet-deploy ENVIRONMENT=develop SOURCE=HEAD INITIAL=1
```

Without `APPLY=1`, bootstrap, configure, and readiness are recorded as
`SKIPPED_DRY_RUN`; no SSH or mutation occurs. Even with explicit apply, the
current coordinator stops at `WAITING_FOR_BACKEND`. It does not apply a backend
manifest, promote DNS/data plane, or update `refs/deployments/*`.

## Generated output

```text
build/<environment>/
├── ansible-inventory.json
├── bootstrap-inventory.json
├── dns-plan.json
├── monitoring-targets.json
├── node-plans/<instance_id>.json
└── impact-plan.json
```

The fleet inventory is always generated from compiled desired state. The only
hand-maintained inventory is the one-host management bootstrap input, tracked
only inside the SOPS-sealed `inventories/bootstrap/platform.sops.yml` bundle.

## Repository map

```text
desired/                 desired infrastructure topology
contracts/               desired-state, backend, and agent contracts
fleetctl/                validation, compiler, planner, adapters, coordinator
playbooks/bootstrap/     clean-host bootstrap
playbooks/deploy/        compiled steady-state configuration
playbooks/operations/    readiness checks
roles/                   reusable Ansible component roles
tests/unit/              offline validation and orchestration tests
docs/                    normative architecture and implementation status
```

## Security boundary

- Never commit resolved secrets, client UUIDs, ready client links, private keys,
  recovery material, or decrypted inventories.
- Desired state contains only `secret://` references.
- Machine WireGuard and agent private keys are generated on the machine and do
  not leave it.
- Provider, DNS, Vault, backend, SSH, and live fleet mutations require separate
  explicit authorization; local validation does not imply that authorization.

See [`docs/README.md`](docs/README.md) for the compact documentation index.
