# Full runtime without access hardening

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
