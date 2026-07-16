# Current operational boundary

The active repository is intentionally a functional, no-hardening deployment. It changes
application/runtime packages and Compose stacks, but refuses to manage:

- SSH users, keys, sudoers or sshd configuration;
- nftables or provider firewall rules;
- Fail2ban;
- security sysctls, auditd or unattended-upgrade policy;
- Docker daemon policy;
- WireGuard or any private management network.

The Xray gRPC API and telemetry ingestion are public and unauthenticated in this phase. Vault
remains loopback-only. This is suitable for integration testing and E2E validation, not a final
security posture.

Before a later hardening project, first preserve a tested break-glass console path, add access
controls one component at a time, verify rollback after each change, and keep application
functionality independent from the management transport.

Runtime API users are ephemeral. The backend must persist and reconcile desired user state.
Back up `/var/lib/xray/reality.key`, platform Docker volumes, and the backend user database.
