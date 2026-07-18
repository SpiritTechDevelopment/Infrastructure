# Security

How the fleet is protected, and where each control is documented. The overriding
principle is **overlay-first exposure**: only `:443` (data) and key-only SSH
(`:22`/`:232`) are public; everything else (Xray API, telemetry, Grafana, Vault) is
reachable **only over the WireGuard overlay**.

## The controls

| Control | What it does | Where |
|---|---|---|
| **WireGuard overlay** | Private management/telemetry plane (`wg0`, `10.20.0.0/24`, hub-and-spoke). Required to reach the API, telemetry, Grafana, Vault. | [WIREGUARD.md](WIREGUARD.md) |
| **Firewall** | Managed nftables on every host; per-host profile decides public vs overlay-only. Docker-NAT-safe. | [ARCHITECTURE.md §6](../architecture/ARCHITECTURE.md#6-firewalls--port-exposure), `roles/common` + `host_vars/*/firewall.yml` |
| **SSH** | Key-only fleet-wide (no source whitelist, no passwords). | [ARCHITECTURE.md §12](../architecture/ARCHITECTURE.md) |
| **Vault SSH CA** | Short-lived (24h) SSH certs, **source-locked to the overlay**, on top of `authorized_keys`. | [VAULT_SSH_CA.md](VAULT_SSH_CA.md) |
| **Secrets** | SOPS-encrypted in Git (secrets + the inventory); private/unseal keys out-of-band. | [OPERATIONS.md §3](../deploy/OPERATIONS.md) |
| **Host hardening** | fail2ban, sysctl, auditd, unattended-upgrades (codified, `deploy_mode`-gated). | [CONVERGENCE_STATUS.md](../status/CONVERGENCE_STATUS.md) |
| **Alerting** | Vault seal, node reachability, telemetry-missing → Telegram. | [ARCHITECTURE.md §11](../architecture/ARCHITECTURE.md) |

## WireGuard overlay — the short version

The overlay is the backbone of the management plane. **You cannot manage the fleet,
reach the Xray API, or view telemetry unless your machine is a `wg0` peer.**

- **Topology:** hub-and-spoke. Hub = `control-1` (`10.20.0.1`); spokes = entry/exit
  (`10.20.0.11`, `10.20.0.21`); operator workstation = `10.20.0.2`. Peers' public keys
  live in `operators` / `management_wireguard_external_peers` in `group_vars/all.yml`.
- **Setup / join:** bring the overlay up on the workstation (`sudo wg-quick up wg0`),
  confirm `ip -br addr show wg0` shows `10.20.0.2/24`. The node side is codified in the
  `management_wireguard` role (`make management`, applied fleet-wide — it restarts
  `wg0`, so do it in a maintenance window over public SSH).
- **Why it matters for security:** it takes the whole control surface off the public
  internet, and the Vault SSH CA even locks issued certs to overlay source addresses —
  so a stolen cert is useless off the overlay.

Full setup, peer roster, codification status, and the re-apply procedure:
**[WIREGUARD.md](WIREGUARD.md)**.

## See also

- Recover from a lost laptop / key: [../deploy/RECOVERY.md](../deploy/RECOVERY.md).
- The full security posture + hardening state: [../status/CONVERGENCE_STATUS.md](../status/CONVERGENCE_STATUS.md).
- Vault (root of trust) responsibilities: [VAULT_SSH_CA.md](VAULT_SSH_CA.md).
