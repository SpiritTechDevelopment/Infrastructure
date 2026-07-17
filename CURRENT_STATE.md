# Current state of the infrastructure

A point-in-time snapshot of the Spirit VPN fleet after the hardening convergence.
For the change history see [RECAP.md](RECAP.md); for what's next see
[NEXT_STEPS.md](NEXT_STEPS.md) and [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md).

## Fleet

| Host | Role | Public IP | SSH | Overlay | Runs |
|---|---|---|---|---|---|
| `control-1` | platform + WG **hub** | 193.247.81.167 | :22 | 10.20.0.1 | Vault, Prometheus, Loki, Grafana, Alertmanager, blackbox, xray-usage-exporter |
| `entry-1` | VPN entry | 5.101.67.252 | :232 | 10.20.0.11 | xray :443 (VLESS/REALITY), nginx-mask, node-exporter, alloy, Xray API :10085 |
| `exit-fr` | VPN exit (fr) | 151.247.196.239 | :22 | 10.20.0.21 | xray :443, nginx-mask, node-exporter, alloy, Xray API :10085 |
| `exit-nl` | exit (nl) | — | — | — | **disabled** (`node_enabled: false`) |

External overlay peers: operator **workstation** `10.20.0.2`, **exit-ru** `10.20.0.23`.
Data plane: `Customer → entry-1:443 (REALITY) → exit-fr:443 → Internet`.

## Network exposure

Only **`:443`** (data plane) and **`:22`/`:232`** (SSH, key-only) are public.
Everything else is **overlay-only** (reachable only as a `wg0` peer):

| Surface | Port | Exposure |
|---|---|---|
| VLESS/REALITY (data) | 443 | public |
| SSH | 22 / 232 | public, **key-only, no source whitelist** |
| Xray gRPC API | 10085 | overlay-only (`10.20.0.0/24`) |
| Prometheus / Loki | 9090 / 3100 | overlay-only |
| Grafana / Alertmanager | 3000 / 9093 | overlay-only |
| Vault | 8200 | **loopback-only** on control-1 |

## Access model

- **SSH: key-only fleet-wide**, no source whitelist. Password auth disabled everywhere.
- **`ansible_user: deploy`** + `sudo` (NOPASSWD) for steady-state deploys; **root is
  key-only break-glass**. `deploy` is a named account (docker + sudo groups).
- **Operator roster** = `operators:` in `inventories/prod/group_vars/all.yml`
  (currently `pavel`, `roman`); their SSH public keys are rendered to
  `authorized_keys` on every host via `playbooks/access.yml`.
- **Vault SSH CA**: 24h certs, `source-address`-locked to the overlay, signed by
  Vault's `ssh-client-signer`; hosts trust the CA (`TrustedUserCAKeys`) **in
  addition to** authorized_keys. See [VAULT_SSH_CA.md](VAULT_SSH_CA.md).
- Management (SSH, Xray API, Grafana/telemetry) requires **WireGuard overlay
  membership**. `control-1`'s public `:22` banner-hangs from the workstation — reach
  it over the overlay (`-e ansible_host=10.20.0.1`).

## Hardening — all codified (`roles/common`, `deploy_mode`-gated) + applied

| Layer | State |
|---|---|
| Firewall | **managed nftables** on all hosts (per-host `host_vars/*/firewall.yml`); Docker-NAT-safe |
| SSH | key-only, no whitelist; Vault-CA cert trust |
| fail2ban | port-aware sshd jail, `ignoreip` = loopback + overlay + workstation |
| sysctl | conservative; **preserves `ip_forward=1`**, omits `rp_filter` |
| auditd | identity/sudoers/sshd/vpn/vault watches (`90-spirit.rules`) |
| unattended-upgrades | security-only, never auto-reboot |
| deploy user | named, docker+sudo, operator-key auth |

## Git / secrets

- **Repo:** `git@github.com:SpiritTechDevelopment/Infrastructure.git`, branch `main`.
- **Secrets:** SOPS-encrypted (`inventories/prod/secrets.sops.yml`: Grafana password,
  TLS cert/key, `entry_service_uuid`); materialized by `make decrypt`. Age recipient:
  operator workstation key. See [OPERATIONS.md](OPERATIONS.md) §3.
- **`.local-secrets/`** now holds only out-of-band break-glass material
  (`vault-init.json` = Vault unseal keys + root token; per-node WireGuard keys).
- **CI:** `.github/workflows/ci.yml` (lint, hosted, no secrets) + `deploy.yml`
  (interim hosted-runner). Deploys currently run from the workstation.
- **Recovery:** `scripts/recovery-bundle.sh` → passphrase-encrypted `recovery/*.age`.

## Live component status

- **Data plane:** healthy — customer E2E passes (entry-1 → exit-fr → egress).
- **Telemetry:** Prometheus/Loki/Grafana fresh over the overlay; both nodes reporting.
- **WireGuard overlay:** live and healthy (hand-configured; role codified but not
  re-applied — see [WIREGUARD.md](WIREGUARD.md)).
- **Vault:** initialized + **currently unsealed**; **no auto-unseal** (re-seals on
  restart); loopback-only; SSH CA configured.
- **Workstation `wg0`:** boot-persistent — `Address = 10.20.0.2/24` under
  `[Interface]` and `wg-quick@wg0` enabled; NetworkManager leaves it
  externally-managed. Survives reboot and half-failed bring-ups.
