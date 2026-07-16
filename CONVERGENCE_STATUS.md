# Hardening Convergence — Status & Resume Guide

**Read this first to resume the security-hardening convergence.** It is
self-contained: current state, decisions, what's done, what's next, the working
discipline, and the hard-won gotchas. Companion docs:
`ONBOARDING_AND_HARDENING.md` (the plan + checklist), `captured-state/FINDINGS.md`
(Phase-0 live findings), `ARCHITECTURE.md` (system overview + runbook).

> **What this project is:** bringing the fleet's host hardening (firewall,
> WireGuard overlay, SSH, etc.) under Ansible management **without breaking the
> live data plane**, and moving management/telemetry off public exposure onto
> the WireGuard overlay ("overlay-first"). The repo previously only *stubbed*
> hardening (a tripwire + orphaned templates); we are making it real, host by
> host, with a strict verify-before-remove discipline.

---

## 1. Fleet quick reference

| Host | Public IP | SSH | Overlay IP | Firewall | Role |
|---|---|---|---|---|---|
| control-1 | 193.247.81.167 | :22 | 10.20.0.1 (hub) | nftables — **codified/managed** | Vault + observability |
| entry-1 | 5.101.67.252 | **:232** | 10.20.0.11 | nftables — **codified/managed** | VPN entry |
| exit-fr | 151.247.196.239 | :22 | 10.20.0.21 | nftables — **codified/managed** | VPN exit (fr) |
| exit-nl | 151.243.176.34 | — | — | — | **disabled** |

- Overlay `wg0` = `10.20.0.0/24`, hub-and-spoke, hub forwards (`ip_forward=1`),
  spoke-to-spoke works. Also on the overlay: **workstation** `10.20.0.2`
  (`206.84.238.130` public), external peer `10.20.0.23` (`91.237.249.103`).
- **The overlay is hand-configured** (the `management_wireguard` role is stubbed
  / `enabled: false`) — not codified yet.
- Data plane (customer): `Customer → entry-1:443 → REALITY → exit-fr:443 → net`.

---

## 2. Decisions locked (do not re-litigate)

- **deploy_mode** gate replaces the old unconditional hardening tripwire:
  `runtime` (app only, refuses hardening) / `bootstrap` / `hardened`.
- **10085 (Xray API): overlay-only.** All consumers use `xray_api_overlay_host`.
- **exit-fr firewall: migrate ufw → nftables** (uniform managed engine).
- **Deploy user: privileged** (docker group + sudo; named/auditable, not a hard
  boundary — accepted).
- **Cert access model:** static break-glass key + Vault SSH CA (24h certs,
  `source-address`-locked to `10.20.0.0/24`) + provider console. *(SSH CA not
  built yet.)*
- **Preflight conflict policy:** detect-and-refuse, opt-in `preflight_auto_clear`.
- **Firewall replacement idiom:** `add table inet filter; delete table inet
  filter; table inet filter {…}` — **NOT `flush ruleset`** (which wipes Docker's
  `ip nat`). `destroy table` is unavailable (nft is **v1.0.2** fleet-wide).

---

## 3. Done (applied to live fleet + committed)

1. **SSH hardened** on control-1 + exit-fr → key-only, no password auth
   (entry-1 was already hardened + fail2ban). Verified no lockout.
2. **Retired control-1's leftover native VPN stack** (native xray:443 +
   nginx:8443 from when it was a hand-managed exit) — freed the ports.
3. **`deploy_mode` gate** + **codified SSH hardening** as a `common` role task
   (`sshd-hardening.conf.j2` + `Reload sshd` handler) via `playbooks/harden.yml`.
   Zero-diff proven against live.
4. **Codified nftables firewall** (`roles/common/templates/nftables.conf.j2`):
   fixed the orphaned template (add+delete idiom for 1.0.2; **added the missing
   Docker-bridge-egress rule**; wired all previously-undefined variables).
   **entry-1 canary applied** — behavior-preserving, full customer E2E passed.
5. **exit-fr onto the overlay** (hand-added `wg0` @ 10.20.0.21; swapped the hub's
   stale reserved peer). Verified handshake + telemetry-port reachability.
6. **B1 — telemetry → overlay:** `telemetry_hub_host: 10.20.0.1`; both nodes'
   metrics + logs verified fresh over `wg0`.
7. **B2 — API consumers → overlay:** `xray_api_overlay_host` per node; repointed
   usage-exporter, blackbox probe, `verify.yml`, `xray-api.sh`, `client-metadata`.
   Workstation onboarded to overlay. Added `XRAY_GRPC_TIMEOUT` (10s) to
   `xray-api.sh` (default 3s was too tight over the overlay).
8. **C1 — entry-1 public 10085 removed** → overlay-only. Verified: 443 public,
   10085 filtered publicly / reachable over overlay, customer E2E passes.
9. **C2 — control-1 firewall codified + public 9090/3100/3000 removed** →
   overlay-only. Profile `inventories/prod/host_vars/control-1/firewall.yml`
   (bridge/DNAT-aware): `common_public_tcp_ports: []`, `common_public_udp_ports:
   [51820]`, `common_restricted_tcp_rules` = `{3100,9090}`←`10.20.0.0/24` (fleet
   telemetry ingest) + `{8200,3000,9093}`←`10.20.0.2/32` (workstation),
   `common_management_forwarding_enabled` + `common_docker_bridge_forwarding`.
   Applied over the overlay with a **Docker-NAT-safe dead-man** (reverted only
   `inet filter` via add+delete, so `ip nat` was never at risk). Zero-diff dry-run
   matched. Verified: public 9090/3000/3100 now **filtered**; overlay 9090/3100/
   3000 reachable; **`ip nat` intact (6 DNAT rules)**; Prometheus fresh (both
   nodes + overlay-10085 probes up), Loki ingesting from all 3 nodes (<4s),
   Grafana healthy over overlay, `api-ping` OK both nodes. Persisted config is
   add+delete (reboot- + NAT-safe); `nftables.service` enabled.
10. **C3 — exit-fr migrated ufw → nftables + public 10085 removed** →
    overlay-only. Profile `inventories/prod/host_vars/exit-fr/firewall.yml`
    (entry-1 model; host-networked data plane so no DNAT forward rules):
    `common_public_tcp_ports: [443]`, `common_restricted_tcp_rules` =
    `{10085}`←`10.20.0.0/24`, `common_docker_bridge_forwarding: true`,
    **`ssh_allowed_cidrs: []`** (SSH open to anyone with a valid key — key-only
    auth is enforced fleet-wide; per operator decision). Engine swap done
    coexistence-safe (loaded `inet filter` *before* `ufw disable`, so no
    unprotected window), over the overlay, with a **ufw-restoring dead-man**.
    Verified: **443 public**, **10085 filtered publicly / overlay-only**, SSH
    open, **customer E2E PASS** (entry-1→exit-fr→egress 151.247.196.239),
    Prometheus (`node=exit-fr` + both blackbox probes up) + Loki (exit-fr <3s)
    fresh, `api-ping` OK. Retired the legacy out-of-repo **`server-stats.service`**
    (gunicorn on public `:8000`) — disabled + stopped. `nftables.service` enabled
    + active; `ufw` disabled at boot.
    - **ufw-leftover caveat:** `ufw disable` sets its chains to policy-accept but
      does **not** unload the `ip filter`/`ip6 filter` tables (they're *shared
      with Docker*, so `nft delete table ip filter` would tear out Docker's chains
      and a `docker restart` would drop the data plane + lose runtime users). The
      leftover ufw chains are non-authoritative (`inet filter` policy-drop wins on
      the shared input hook) and clear on next reboot (ufw is disabled at boot).
      Did **not** force-remove them.
    - **Also on exit-fr:** a second out-of-repo unit `lenza-telegram-export.service`
      ("Lenza Telegram Export API") is running but not on any public port — left
      untouched.
11. **SSH source whitelist removed fleet-wide** (operator decision). Set
    `ssh_allowed_cidrs: []` on control-1 + entry-1 (exit-fr already open) and
    re-applied the managed firewall (dry-run + Docker-safe dead-man + verify +
    commit, over the overlay). Effective SSH rule is now `tcp dport <port> accept`
    with no source scope on every host (control-1/exit-fr `:22`, entry-1 `:232`);
    security is **key-only auth** (passwords already disabled fleet-wide).
    Verified all three open, control-1 `ip nat` still intact (6 DNAT), entry-1
    443-public/10085-overlay-only preserved.

---

## 4. NEXT — remaining work (start here)

**All of C1/C2/C3 (overlay-first exposure rollback) are done.** The fleet's
management/telemetry/API surfaces are overlay-only; only `:443` (data plane) and
`:22` (key-only SSH) are public fleet-wide. What remains is the deferred/future
hardening below — none of it blocks the overlay-first goal.

### Done (host hardening — L6)
- **fail2ban / sysctl / auditd / unattended-upgrades** codified in `roles/common`
  (deploy_mode-gated) and applied fleet-wide. fail2ban: port-aware sshd jail,
  systemd backend, **`ignoreip` = loopback + overlay + workstation** (an SSH client
  offering multiple agent keys logs "Failed publickey" and can trip fail2ban — the
  ignoreip prevents operator lockout). sysctl: conservative hardening that
  **preserves `ip_forward=1`** (Docker + WG hub) and omits `rp_filter`. auditd:
  wired `audit.rules.j2`; the managed `90-spirit.rules` **replaced the hand-placed
  duplicate `50-vpn.rules`** (the dup made `augenrules --load` fail "Rule exists");
  added an "ensure loaded" task so an interrupted run can't leave rules unloaded.
  unattended-upgrades: security-only, never auto-reboot. Verified fleet-wide;
  data plane + overlay + telemetry intact.
  Apply: `harden.yml -e deploy_mode=hardened -e common_enable_fail2ban=true
  -e common_manage_sysctl=true -e common_enable_auditd=true
  -e common_enable_unattended_upgrades=true --limit <host>`.

### Deferred / future
- **Codify the WireGuard overlay** via `management_wireguard` role with zero-diff
  (currently hand-configured); un-stub `make management`; model external peers
  (10.20.0.2 workstation, 10.20.0.23) in `management_wireguard_external_peers`.
- **Vault SSH CA** (24h, source-address-locked) + full Vault production init.
- **`deploy` user creation** (`common_manage_deploy_user`) — named non-root account.
- **Workstation:** fix `/etc/wireguard/wg0.conf` so `wg0` gets its address on
  boot (it has `Address = 10.20.0.2/32` but a half-failed bring-up skipped it;
  clean `wg-quick down/up` fixes it). Enable local docker on boot.
- **Rotate** the workstation WG private key (pasted in a prior chat) when convenient.

---

## 5. Working discipline (follow for every host change)

1. **Repoint every consumer to the overlay BEFORE removing public access.**
2. **Firewall apply = dead-man switch.** Before applying, on the host:
   ```
   cp -a /etc/nftables.conf /etc/nftables.conf.preconverge
   rm -f /tmp/fw-converge-ok
   setsid bash -c "sleep 300; [ -f /tmp/fw-converge-ok ] || nft -f /etc/nftables.conf.preconverge" >/dev/null 2>&1 </dev/null &
   ```
   Apply → verify (SSH + data plane + E2E) → `touch /tmp/fw-converge-ok` to
   commit (or let it auto-revert).
3. **SSH has no source whitelist fleet-wide** (operator decision) — `:22`/`:232`
   accept from anywhere, gated by **key-only auth** (password/keyboard-interactive
   disabled everywhere). So a firewall change can't lock out SSH by source; the
   provider console remains the hard fallback. (`ssh_allowed_cidrs: []` on every
   host.)
4. **Reconcile runtime users after ANY xray restart** (`make reconcile …` or
   re-add) — they are in-memory only.
5. **Apply firewall via:** `ansible-playbook -i inventories/prod/inventory.yml
   playbooks/harden.yml -e deploy_mode=hardened -e common_manage_firewall=true
   --limit <host>` (dry-run first with `--check --diff`).
6. **Verify after each step:** `make e2e-all ENTRY=entry-1`, `make api-ping`,
   Prometheus/Loki freshness, `nft list ruleset`.

---

## 6. Gotchas (hard-won)

- **nft is v1.0.2** — no `destroy table`; use add+delete. The managed template
  already does this.
- **`flush ruleset` wipes Docker's `ip nat` on control-1** (bridge host). The
  managed template avoids it; if you *hand-edit* control-1's firewall the old
  way, run `systemctl restart docker` after.
- **Forward chain needs `ip saddr 172.16.0.0/12 accept`** (Docker bridge egress)
  or bridge-networked containers silently fail. In the template via
  `common_docker_bridge_forwarding`.
- **Prometheus scrape_config changes need a restart** — the observability role
  has a `Restart Prometheus` handler; `file_sd` targets auto-reload.
- **Overlay latency** → Xray gRPC calls need `-timeout` (via `XRAY_GRPC_TIMEOUT`,
  already in `xray-api.sh`).
- **Workstation must have `wg0` up with its address** to manage over the overlay;
  **local docker** must be running for `xray-api.sh`/`gen-client` (not enabled on
  boot).
- **SSH is key-only fleet-wide with NO source whitelist** (`ssh_allowed_cidrs:
  []` on all hosts) — control-1/exit-fr `:22`, entry-1 `:232`, all accept from
  anywhere, valid key required.
- **control-1 public :22 is flaky from the workstation** — TCP connects but the
  SSH banner exchange hangs (public ping to `193.247.81.167` is ~0.4ms while the
  overlay RTT is ~270ms, i.e. something local answers for the public IP). The
  **reliable admin path to control-1 is the overlay** (`ansible_host=10.20.0.1`,
  with a separate `UserKnownHostsFile` since the overlay host key differs). This
  narrows the anti-lockout margin — provider console is the hard fallback.
- **Dead-man revert must itself be Docker-NAT-safe.** §5.2's literal snippet
  copies the *current* `/etc/nftables.conf`, which on control-1 used
  `flush ruleset` — reverting with it would wipe Docker's `ip nat`. Instead build
  the revert from the live table: `{ echo 'add table inet filter'; echo 'delete
  table inet filter'; nft list table inet filter; } > /tmp/fw-revert.nft` — it
  reverts only `inet filter` and leaves `ip nat` untouched.

---

## 7. Key files

- `roles/common/{defaults,tasks,handlers}/main.yml` — deploy_mode, sshd + firewall tasks.
- `roles/common/templates/{sshd-hardening.conf.j2, nftables.conf.j2}` — managed configs.
- `playbooks/harden.yml` — the hardening stage.
- `inventories/prod/host_vars/entry-1/firewall.yml` — **the model firewall profile** for C2/C3.
- `inventories/prod/group_vars/all.yml` — `telemetry_hub_host`.
- `inventories/prod/inventory.yml` — `xray_api_overlay_host` per host.
- `captured-state/` — Phase-0 live snapshots (incl. `etc-nftables.conf` per host) + `FINDINGS.md`.
- `ONBOARDING_AND_HARDENING.md` — full plan + live checklist.
