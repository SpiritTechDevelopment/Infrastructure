# Recap — hardening convergence

Everything done to bring the fleet's hardening, exposure, access, and operations
under Ansible + Git, without breaking the live data plane. Current snapshot:
[CURRENT_STATE.md](CURRENT_STATE.md). Detailed per-area docs are cross-referenced.

## 1. Overlay-first exposure rollback (C1–C3)

Moved every management/telemetry/API surface off the public internet onto the
WireGuard overlay, verify-before-remove, each with a dead-man auto-revert:

- **B1/B2** — telemetry (metrics/logs) and all Xray-API (`10085`) consumers
  repointed to overlay addresses (`telemetry_hub_host`, `xray_api_overlay_host`).
- **C1 — entry-1**: public `10085` removed → overlay-only.
- **C2 — control-1**: firewall codified; public `9090/3100/3000` removed →
  overlay-only (bridge/DNAT-aware forward rules; `ip nat` preserved).
- **C3 — exit-fr**: migrated **ufw → managed nftables**; `10085` overlay-only;
  retired a legacy out-of-repo `server-stats.service` (public `:8000`).
- Firewall engine now **nftables fleet-wide**, managed by `roles/common` from
  per-host `host_vars/*/firewall.yml` (Docker-NAT-safe add+delete idiom).

## 2. SSH posture

- Key-only fleet-wide (password auth disabled everywhere).
- **Removed the SSH source whitelist** — `:22`/`:232` accept from anywhere, gated
  purely by key-only auth (operator decision).

## 3. Git + secrets + operations model

- **Initial commit + pushed** to GitHub (`SpiritTechDevelopment/Infrastructure`).
  Audited history — no secrets; unstaged real-secret backups before the first commit.
- **SOPS** wired (age): `secrets.sops.yml` (Grafana pw, TLS cert/key,
  `entry_service_uuid`), `make decrypt`; `.local-secrets` reduced to break-glass only.
- **Operator roster** consolidated (`operators:` — ssh/wg/age public keys per person);
  `playbooks/access.yml` renders `authorized_keys` (scoped, non-disruptive).
- **Recovery bundles** — passphrase-encrypted (`age -p`) crown-jewel backup so a lost
  laptop is survivable; only `recovery/*.age` is committable.
- **CI** — `ci.yml` (lint, hosted) + `deploy.yml` (interim hosted-runner); `CODEOWNERS`;
  `OPERATIONS.md`, `CUTOVER.md`, `RECOVERY.md` written.
- **Docs refreshed** to current state (`ARCHITECTURE.md`, `WIREGUARD.md`, `FIRST_RUN.md`,
  API/BACKEND/TESTING); obsolete "no-hardening" docs banner-marked SUPERSEDED.

## 4. Host hardening (L6) — codified + applied fleet-wide

`roles/common`, `deploy_mode`-gated:

- **fail2ban** — port-aware sshd jail; **`ignoreip`** (loopback + overlay + workstation)
  so agent-key-spam can't lock out an operator.
- **sysctl** — conservative; **preserves `ip_forward=1`** (Docker + WG hub), omits
  `rp_filter`.
- **auditd** — wired the orphaned `audit.rules.j2`; removed a byte-identical
  hand-placed `50-vpn.rules` duplicate.
- **unattended-upgrades** — security-only, never auto-reboot.

## 5. Deploy user (L5)

- Named `deploy` account (docker + sudo, NOPASSWD sudoers, operator-roster keys),
  reconciled fleet-wide (created on exit-fr; docker group added on control-1/entry-1).
- **Migrated `ansible_user` root → deploy** in `group_vars/all.yml`
  (`ansible_user: deploy` + `ansible_become: true`; verified — remote plays escalate,
  localhost plays don't). Root stays key-only break-glass.

## 6. WireGuard overlay codified (L4)

- Un-stubbed `roles/management_wireguard` + `playbooks/management-network.yml` +
  `make management`. Inputs in `group_vars/all.yml` (addresses, hub endpoint,
  external peers). Offline render **proven functionally identical** to the live
  `wg0.conf` on all hosts. **Not yet re-applied live** (first run restarts `wg0`).
  See [WIREGUARD.md](WIREGUARD.md).

## 7. Vault SSH CA (L5 access)

- **Unsealed Vault** (was sealed). Configured the `ssh-client-signer` CA + roles
  `operator` (24h, source-address-locked to the overlay, permit-pty, rsa-sha2-256)
  and `automation` (15m). Host trust via `TrustedUserCAKeys` (`roles/common`),
  applied fleet-wide. `userpass` + `ssh-operator` policy.
- **Proven end-to-end**: cert-only login (throwaway key not in authorized_keys)
  works over the overlay and is `Permission denied` from a public source (overlay
  lock). Codified in `roles/vault` (`vault-ssh-ca.sh`). Bootstrap root-token file
  shredded. See [VAULT_SSH_CA.md](VAULT_SSH_CA.md).

## Locked decisions (do not re-litigate)

- `deploy_mode` gate (`runtime`/`bootstrap`/`hardened`) replaced the old tripwire.
- Firewall replacement idiom = `add table … delete table … table {…}` (nft 1.0.2,
  Docker-NAT-safe), **never `flush ruleset`**.
- Deploy user is **privileged** (docker+sudo = root-equivalent) — accepted as a
  named/auditable account, not a hard boundary.
- SSH certs are **overlay-only** (`source-address` lock earns the 24h TTL).
- SSH has **no source whitelist** — key-only is the gate.
- Secrets = **SOPS-encrypted in Git**; private keys / unseal keys **never** in Git.
