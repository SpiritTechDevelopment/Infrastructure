# Node Onboarding & Security Hardening Plan

Consolidates the design decisions for (a) onboarding a new server into the
fleet and (b) bringing host hardening under Ansible management without breaking
the running data plane. Read `ARCHITECTURE.md` first for the current state.

> **Framing.** Today the repo deploys the *application* cleanly but **does not
> implement host hardening** — the hardening flags are a tripwire and the
> `nftables`/`tc`/`audit` templates are orphaned (no task renders them). The
> live hosts are hardened by hand and have drifted. This plan is therefore a
> **build + convergence** effort, not a config change. The guiding constraint
> throughout: **never break the data plane and never lock yourself out.**

## Table of contents

1. [Design decisions (settled)](#1-design-decisions-settled)
2. [Two deploy modes](#2-two-deploy-modes)
3. [Access & anti-lockout model](#3-access--anti-lockout-model)
4. [Node onboarding workflow (bootstrap)](#4-node-onboarding-workflow-bootstrap)
5. [Steady-state operations](#5-steady-state-operations)
6. [Convergence plan for existing live hosts](#6-convergence-plan-for-existing-live-hosts)
7. [Blocker-avoidance invariants](#7-blocker-avoidance-invariants)
8. [Preflight / conflict checklist](#8-preflight--conflict-checklist)
9. [Prerequisites, risks & open items](#9-prerequisites-risks--open-items)
10. [Implementation checklist](#10-implementation-checklist)

---

## 1. Design decisions (settled)

- **Isolation mechanism = WireGuard**, not Xray/REALITY. REALITY solves a
  hostile-DPI problem that does not exist between our own hosts; Xray is a
  larger attack surface and reusing the internet-facing data-plane proxy as the
  control-plane fabric would collapse data/control isolation. Keep the planes
  separate: Xray/REALITY on public `:443` (data), WireGuard `10.20.0.0/24`
  (management, nothing public).
- **Overlay-first exposure.** Management/telemetry ports bind to the WireGuard
  overlay; only `:443` is public. The current public openings of `10085`,
  `9090`, `3100`, `3000` (bring-up stopgaps) are **rolled back** during
  convergence, and `verify.yml` is updated to reach telemetry over the overlay.
- **Named privileged deploy user.** Steady-state deploys run as a non-root
  `deploy` user (Docker access + `sudo`) using a key/cert; root password is used
  only for the first bootstrap and disabled by that same run. On a Docker host
  this makes `deploy` effectively root-equivalent — the win is a **named,
  auditable account with no root-password login**, not a hard privilege
  boundary. Accepted deliberately.
- **Keep root as break-glass, key-only.** Hardening drops root *password* auth
  (`PermitRootLogin prohibit-password`), not the root account. Root SSH is
  ideally restricted to the overlay/admin IP.
- **Access via Vault SSH CA** in steady-state (see [§3](#3-access--anti-lockout-model)):
  24h certs, `source-address`-locked to the overlay. Static break-glass key +
  provider console remain as fallbacks.
- **Vault holds shared/recoverable material only** — public keys, host-key
  fingerprints, the SSH CA, deploy credentials. **Per-node private keys
  (SSH host, WireGuard) are generated on the node and never leave it.**
- **`deploy_mode` replaces the tripwire.** A mode variable gates hardening
  (see [§2](#2-two-deploy-modes)) instead of the current unconditional refusal.

---

## 2. Two deploy modes

Replace the unconditional assert in `roles/common/tasks/main.yml` ("Refuse
access and host-hardening changes in runtime mode") with a `deploy_mode`
variable:

| Mode | Runs | When |
|---|---|---|
| `runtime` (default) | application only (today's behavior) | routine deploys |
| `bootstrap` | application + full hardening + deploy user + WireGuard | onboarding a new node |
| `hardened` | application + idempotent re-assertion of hardening | steady-state on hardened nodes |

The hardening flags (`common_manage_firewall`, `common_manage_sshd`,
`common_enable_fail2ban`, `common_manage_sysctl`, `common_enable_auditd`,
`common_enable_unattended_upgrades`, `common_manage_deploy_user`,
`management_wireguard_enabled`) become **effective only when `deploy_mode` is
`bootstrap`/`hardened`**, and the guard asserts they stay false in `runtime`.

---

## 3. Access & anti-lockout model

Layered so no single failure locks you out:

```
static break-glass key (bootstrap + emergencies, offline-stored)
        │
        ▼
Vault SSH CA  →  24h certs, source-address-locked to 10.20.0.0/24  (steady-state)
        │
        ▼
provider console / KVM  (out-of-band hard fallback, always works)
```

**Vault SSH CA (steady-state login):**
- Vault SSH secrets engine in CA mode (`ssh-client-signer`); each host trusts
  the CA via `TrustedUserCAKeys`.
- Operator authenticates to Vault → Vault signs the operator's **public** key
  into a short-lived certificate → operator SSHes with the cert.
- Vault role settings:
  - `ttl = 24h`, with a `max_ttl` cap (human convenience: sign once/day).
  - **`critical_options.source-address` = `10.20.0.0/24`** (overlay-only — your
    workstation must be a WireGuard peer to log in). This is what *earns* the
    24h TTL: a leaked cert is useless off the private overlay. Break-glass key +
    provider console remain the non-overlay fallbacks.
  - Tight `valid_principals` (`deploy`, `root`), minimal `extensions`
    (`permit-pty`; no port/agent forwarding unless needed).
  - A **separate short-TTL role (minutes)** for automation/CI, which mints a
    fresh cert per run and never needs 24h.
- **Revocation caveat:** SSH has no CRL/OCSP; revocation means distributing a
  KRL to every host (clunky). So expiry is the primary control — which is
  exactly why the `source-address` lock matters with a 24h TTL.

**Anti-lockout rules (non-negotiable):**
1. A **static break-glass root key**, offline-stored, never rotated away.
2. **Provider console confirmed working** before hardening anything.
3. **apply → verify → commit** for every access change: prove the new path
   before removing the old.
4. Operator/deploy **public** keys tracked in Vault; **private keys never
   centralized**.

---

## 4. Node onboarding workflow (bootstrap)

Prerequisite: `control-1` + Vault already bootstrapped (root of trust; see §9).

One-time, `deploy_mode: bootstrap`, run interactively:

1. **Add to inventory** with `node_enabled: false` until proven.
2. **Connect as root via interactively-entered password**
   (`SSH_AUTH=password ASK_PASS=1`). The password is **never** stored in
   config/git; retrieved from the operator, or from Vault if a prior value
   exists.
3. **Preflight / clear conflicts** (see [§8](#8-preflight--conflict-checklist)) —
   fail fast on port/service/firewall conflicts. Disable leftover native
   services (`nginx`, `xray`, `wireguard`) that would squat on `:443`/`:8443`.
4. **Create `deploy` user**, generate + install its SSH key (or configure the
   Vault SSH CA trust), **verify key/cert login works**.
5. **Generate WireGuard keypair on-node**; publish only the public key.
6. **Harden** (`apply → verify → commit` each): disable root password auth
   (keep root key-only), Docker-aware firewall, sysctl, auditd, fail2ban,
   unattended-upgrades.
7. **Activate WireGuard** — re-render the **whole fleet** (the role asserts a
   no-`--limit` run so the hub peer list includes every active host).
8. **Store shared/recoverable material in Vault** (public keys, host-key
   fingerprints). Private keys stay on-node.
9. **Flip inventory** to `ansible_user: deploy` + cert/key auth; set
   `node_enabled: true`; add to telemetry + WG peer sets.
10. **Verify** the node end to end (data plane if entry/exit, telemetry,
    firewall/WG invariants) before declaring it live.

---

## 5. Steady-state operations

- `deploy_mode: runtime` (or `hardened`), `ansible_user: deploy`, cert/key auth,
  no root, no password.
- All deploys idempotent (`make deploy` / `make apply-node LIMIT=…`).
- After any Xray container restart, **reconcile runtime users**
  (`make reconcile …`) — they are in-memory and lost on restart.
- Access via a Vault-signed 24h cert; break-glass key only for emergencies.

---

## 6. Convergence plan for existing live hosts

The live `control-1`/`entry-1`/`exit-fr` are already in production with
hand-rolled hardening. Bring them under management **without breaking the data
plane**, in phases, each independently revertible and verify-gated:

- **Phase 0 — Capture.** Snapshot each host's live `nftables`/`ufw` ruleset,
  `wg0` config, sshd config, and native services into the repo as the starting
  templates. No behavior change.
- **Phase 1 — Codify + dry-run.** Turn the captured state into
  `deploy_mode`-gated tasks that render the *same* rules. Run in check/diff mode
  (`--check --diff`) and confirm **zero diff** against live. This proves the
  repo can reproduce the current state before it changes anything.
- **Phase 2 — Mode switch.** Introduce `deploy_mode`; replace the tripwire.
  Still no behavior change (rules identical to captured).
- **Phase 3 — Docker-aware firewall.** Wire the orphaned `nftables.conf.j2`
  into real tasks that include Docker-bridge egress and reconcile Docker after
  reload on bridge hosts (control-1). Apply per-host, verify, commit.
- **Phase 4 — Overlay-first exposure.** Move `10085`/`9090`/`3100`/`3000` off
  public and onto the overlay; update `verify.yml` to reach telemetry over
  `wg0`. Verify E2E still green.
- **Phase 5 — Deploy user + access.** Create `deploy` user, stand up the Vault
  SSH CA, migrate `ansible_user` from root to deploy, drop root password auth.
- **Phase 6 — Remaining hardening.** sysctl/auditd/fail2ban/unattended-upgrades,
  idempotent, verify-gated.

Order hosts **exits → entries → control** or one canary node first, so a mistake
never takes out the whole fleet at once. Reconcile runtime users after any Xray
restart in a phase.

---

## 7. Blocker-avoidance invariants

Encoded lessons from live operation (see `ARCHITECTURE.md` §13):

1. **Docker-aware firewall.** Include `ip saddr 172.16.0.0/12 accept` in the
   `forward` chain; on bridge hosts (control-1) run `systemctl restart docker`
   after any `nft -f` reload (it flushes Docker's `ip nat` table).
2. **Overlay-first exposure**, not public openings.
3. **Never drop the active admin path** — apply → verify → commit, with rollback.
4. **Reconcile runtime users after any Xray restart.**
5. **Verify-gate hardening invariants**: SSH reachable, firewall loaded, `wg0`
   up, Docker NAT intact, data-plane E2E passing.

---

## 8. Preflight / conflict checklist

Extend `playbooks/preflight.yml`. **Policy: detect-and-refuse** — fail with a
clear message and let the operator resolve, *unless* `preflight_auto_clear:
true` is set (opt-in), which auto-disables conflicting native services for
fleets known to be dedicated/clean. Checks:

- **Port conflicts:** `443`, `8443` (mask), `10085` (Xray API), `9090`/`3100`/
  `3000`/`9093`/`8200` (platform), `51820` (WG).
- **Conflicting native services:** `nginx`, `xray`/`v2ray`, host `wireguard`
  (the exact squatters disabled by hand during bring-up).
- **Firewall sanity:** existing rules that would block the above; ability to
  reload without stranding Docker.
- **Docker:** engine present, daemon running, compose v2 available, sane
  `daemon.json`.
- **Resources:** disk/memory headroom for the stack.
- **Time sync:** chrony active (cert validity + REALITY depend on clock).

---

## 9. Prerequisites, risks & open items

**Prerequisites (must exist before the above works):**
- **Vault production-ready** — initialized, **unsealed**, policies + an operator
  auth method configured. Today verify treats Vault as possibly still
  sealed/uninitialized, so this is its own prerequisite project.
- **Root of trust:** `control-1` (which runs Vault) is bootstrapped **first**,
  secured by the static break-glass key. Vault's **unseal keys + root token
  live out-of-band**, never in Vault.

**Risks:**
- The **first cutover** (Phase 3/5) is the only genuinely risky step — managed
  rules/access replacing hand-rolled ones under a live fleet. Mitigated by
  Phase 1's zero-diff proof, canary-first ordering, and apply-verify-commit.
- **`docker` group = root-equivalent** — the `deploy` user is not fully
  de-privileged on a Docker host. **Accepted** (named/auditable account, no
  root-password login; not a hard boundary).
- **SSH cert revocation is weak** — rely on TTL + `source-address` lock; keep
  KRL as break-glass only.

**Resolved decisions:**
- `deploy` privilege: **privileged** (Docker access + sudo; root-equivalent on
  a Docker host, accepted as a named/auditable account rather than a hard boundary).
- Cert `source-address`: **overlay-only** (`10.20.0.0/24`); the workstation must
  be a WireGuard peer to use certs; break-glass key + console are the fallbacks.
- Preflight conflict policy: **detect-and-refuse** by default, with opt-in
  `preflight_auto_clear` for known-clean fleets.

---

## 10. Implementation checklist

Most of this is **net-new** (the repo currently only stubs it):

- [x] Add `deploy_mode` variable; make the `roles/common` guard conditional on
      it. **Done** — `deploy_mode: runtime|bootstrap|hardened`; tripwire now
      gated `when: deploy_mode == 'runtime'` (verified it still refuses in
      runtime and skips in hardened).
- [x] `playbooks/harden.yml` — first-class hardening stage running the
      `common` role. **Done.**
- [ ] Implement `roles/common` hardening tasks (currently only a tripwire):
  - [~] Wire `nftables.conf.j2` into a Docker-aware firewall task. **Canary
        done (entry-1).** Fixed the orphaned template: `destroy table` (needs
        nft≥1.0.3) → `add`+`delete` idiom (1.0.2-compatible, preserves Docker's
        `ip nat` — no `docker restart`); added the missing Docker-bridge-egress
        rule; wired all variables (were undefined). Applied to entry-1 with a
        dead-man auto-revert; behavior-preserving (verified via diff + SSH/443/
        10085 + full customer E2E). **control-1 done** (C2): codified + public
        `9090/3100/3000` removed, overlay-only, `ip nat` intact. **Remaining:**
        exit-fr (C3 — ufw→nftables engine swap, gated on customer E2E).
  - [x] sshd management (root key-only, disable password auth). **Done** —
        `sshd-hardening.conf.j2` drop-in + `Reload sshd` handler (validate then
        reload). Hand-applied to control-1/exit-fr, then codified and **proven
        zero-diff** via `harden.yml --check --diff`.
  - [ ] fail2ban, sysctl, auditd (`audit.rules.j2`), unattended-upgrades.
  - [ ] `deploy` user creation + key/CA trust (`common_manage_deploy_user`).
- [ ] Un-stub `make management`; enable `management_wireguard` in prod; verify
      hub peer re-render on onboarding.
- [ ] Vault: production init/unseal/policies; SSH secrets engine (CA mode);
      human 24h role (source-address-locked) + short-TTL automation role;
      store public keys / host-key fingerprints.
- [ ] Extend `playbooks/preflight.yml` with the [§8](#8-preflight--conflict-checklist) checklist.
- [ ] Extend `playbooks/verify.yml` with hardening invariants ([§7](#7-blocker-avoidance-invariants).5).
- [x] Roll back public exposure of `10085`/`9090`/`3100`/`3000`; move telemetry
      onto the overlay. **DONE (C1/C2/C3):**
  - [x] Telemetry (metrics/logs) repointed to the hub's overlay IP `10.20.0.1`
        (`telemetry_hub_host`); verified fresh over `wg0` from both nodes.
  - [x] All Xray-API (10085) consumers repointed to overlay
        (`xray_api_overlay_host`): usage-exporter, blackbox probe, `verify.yml`,
        `xray-api.sh`, `client-metadata`. Workstation on overlay (`10.20.0.2`).
  - [x] `xray-api.sh`: added `XRAY_GRPC_TIMEOUT` (default 10s) — the default 3s
        Xray gRPC timeout was too tight over the higher-latency overlay path.
  - [x] **entry-1**: public `10085` removed → overlay-only
        (`10.20.0.0/24` via `wg0`); verified (443 public, 10085 filtered
        publicly / reachable over overlay, customer E2E passes).
  - [x] **control-1**: codify firewall + remove public `9090/3100/3000`
        (overlay-only). Bridge/DNAT host → `common_restricted_tcp_rules` emit
        forward-chain `ct status dnat` rules for the published platform ports
        (`{3100,9090}`←overlay `/24`, `{8200,3000,9093}`←workstation), plus hub
        `management_forwarding` + docker-bridge egress. Profile
        `inventories/prod/host_vars/control-1/firewall.yml`. Applied over the
        overlay with a Docker-NAT-safe dead-man; verified public ports filtered,
        overlay ports reachable, `ip nat` intact (6 DNAT rules), telemetry fresh
        (Prometheus/Loki/Grafana), `api-ping` OK.
  - [x] **exit-fr**: migrated ufw→nftables + overlay-only `10085`. Host-networked
        data plane (no DNAT rules) → entry-1-model profile
        `inventories/prod/host_vars/exit-fr/firewall.yml`: public `[443]`, `10085`
        over `wg0` from `10.20.0.0/24`, `ssh_allowed_cidrs: []` (SSH open to any
        valid key). Engine swap done coexistence-safe (loaded `inet filter` before
        `ufw disable`), with a ufw-restoring dead-man; customer E2E passed; 443
        public / 10085 overlay-only verified. Retired legacy out-of-repo
        `server-stats.service` (public gunicorn `:8000`). ufw's shared-with-Docker
        `ip filter` tables left in place (non-authoritative; clear on reboot).
- [ ] Phase-execute the [§6](#6-convergence-plan-for-existing-live-hosts) convergence, canary-first, zero-diff-gated.

---

*See also: `ARCHITECTURE.md` (current state & runbook), `BACKEND_INTEGRATION.md`
(runtime-user contract), `governance/` (logging/data policy).*
