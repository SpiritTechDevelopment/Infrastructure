# WireGuard status

WireGuard/private management networking is intentionally not implemented in the active
repository.

```bash
make management
```

always exits with an error, and `playbooks/management-network.yml` contains only a refusal
stub. No inventory variable can enable a hidden WireGuard role.

Do not copy commands from `docs/legacy/` into production. A future management-network change
must be designed and tested separately from SSH, firewall, API, telemetry and application
deployment, with an independent rollback path.
