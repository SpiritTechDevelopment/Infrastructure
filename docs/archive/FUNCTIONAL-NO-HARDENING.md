# Full runtime without access hardening

> **⚠ SUPERSEDED (historical).** This describes the earlier *functional
> no-hardening* bring-up phase, which is over. The fleet is now hardened and
> overlay-first: the firewall is codified (managed nftables fleet-wide), SSH is
> key-only, and the Xray API + telemetry are **overlay-only, not public**. For
> current state read [CONVERGENCE_STATUS.md](../status/CONVERGENCE_STATUS.md),
> [OPERATIONS.md](../deploy/OPERATIONS.md), and [ARCHITECTURE.md](../architecture/ARCHITECTURE.md). The text
> below is kept only as a record of that phase.

This is a complete functional mode, not a reduced bootstrap.

`playbooks/site.yml` deploys platform services, exits, generated entry routing, entries,
public Xray API, logs, metrics, dashboards and verification. It intentionally leaves
users, SSH keys, sudoers, sshd, host/provider firewalls, fail2ban, security sysctls,
auditd, unattended upgrades, Docker daemon configuration and WireGuard untouched.

Normal deployment also does **not** remove legacy hardening files. Any recovery cleanup
must be performed explicitly by the operator outside the application deployment, so a
future run cannot unexpectedly alter access.

Use `make deploy-e2e` as the acceptance command. Private networking and access hardening
must remain separate until independently designed, tested and reviewed.
