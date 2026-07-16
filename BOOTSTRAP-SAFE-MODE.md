# Functional no-hardening mode

> **⚠ SUPERSEDED (historical).** The "no-hardening" phase is over — the fleet is
> now hardened and overlay-first (codified firewall, key-only SSH, overlay-only API
> + telemetry). See [CONVERGENCE_STATUS.md](CONVERGENCE_STATUS.md) and
> [OPERATIONS.md](OPERATIONS.md). Text below kept as a record.

The repository now deploys the complete service rather than a reduced bootstrap. It installs
runtime dependencies, Docker Compose stacks, Xray, the public gRPC API, Vault, logs, metrics
and dashboards, while refusing all access-control/hardening changes.

Normal deployment neither creates nor removes legacy SSH/firewall/Fail2ban rules. Clean up any
old host policy manually before deployment when necessary. The application playbooks will not
silently change access again.

Use `make deploy-e2e` as the acceptance command.
