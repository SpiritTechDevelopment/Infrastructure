# WireGuard status

The WireGuard overlay (`wg0`, `10.20.0.0/24`, hub `control-1` = `10.20.0.1`) is
**live and required for the management plane**. After the overlay-first hardening,
the Xray API (`10085`), telemetry ingest (`9090`/`3100`), and operator access to
Grafana/Vault are reachable **only** over it — you must be a `wg0` peer to operate
the fleet. See [ARCHITECTURE.md](ARCHITECTURE.md) §7 and [OPERATIONS.md](OPERATIONS.md).

**The Ansible role is still stubbed, though.** The overlay is currently
**hand-configured** on the hosts; the repo does not yet reconcile it:

```bash
make management
```

still exits with an error, and `playbooks/management-network.yml` is a refusal
stub. `management_wireguard_enabled: false` in prod. So `make deploy` does **not**
manage `wg0` — peers are added by hand on the hub.

**Deferred work:** un-stub `roles/management_wireguard` so the overlay is codified
(peers as data in `operators` / `management_wireguard_external_peers`, zero-diff
against live), then enable it. Track this in [CONVERGENCE_STATUS.md](CONVERGENCE_STATUS.md).
Do not copy commands from `docs/legacy/` into production; design any overlay
automation change with its own dry-run + rollback, separate from SSH/firewall/app.
