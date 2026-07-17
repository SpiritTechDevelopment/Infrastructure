# Open questions / pending decisions

Decisions that gate the remaining work ([NEXT_STEPS.md](NEXT_STEPS.md)). Each notes
the trade-off and a recommendation.

## 1. Vault auto-unseal strategy?

Vault re-seals on every restart (manual 3-of-5 unseal). For a CA that hosts rely on,
that's fragile.
- **Options:** (a) auto-unseal via **transit** (a second Vault/OpenBao) or **cloud KMS**;
  (b) accept manual unseal with a documented runbook + alerting on `sealed`.
- **Trade-off:** auto-unseal removes the manual step but adds a dependency (KMS or a
  second Vault) that holds the unseal capability. Manual keeps the 3-of-5 human control
  but means downtime until someone unseals.
- **Recommendation:** if the SSH CA becomes load-bearing, do transit/KMS auto-unseal;
  otherwise the manual runbook is fine short-term. **Blocks NEXT_STEPS #3, #5.**

## 2. Expose Vault on the overlay for remote operator signing?

Vault is `127.0.0.1`-only, so operators sign SSH certs by SSHing to control-1 first.
- **Options:** (a) keep loopback (sign on control-1 — the authorized_keys are the
  bootstrap); (b) bind Vault on the overlay (`vault_bind_address`) so operators sign
  from their own machine — the firewall already permits `8200` from the workstation
  over `wg0`.
- **Trade-off:** exposing Vault requires a **redeploy = restart = re-seal** (pair with
  #1), and widens Vault's surface to the overlay (still private, still Vault-authed).
- **Recommendation:** worthwhile once auto-unseal exists; until then, loopback +
  sign-on-control-1 is acceptable. **Gates NEXT_STEPS #5.**

## 3. CI deploy actor — hosted runner, self-hosted, or workstation?

Today: hosted runner for lint; deploys from the workstation.
- **Options:** (a) workstation deploys (current — simplest, no cloud secrets, weaker
  central audit); (b) **self-hosted runner** on the overlay (audited, needs a box +
  keys on it); (c) hosted runner joining the overlay via cloud-stored keys (documented
  as a downgrade — avoid).
- **Recommendation:** move to a **dedicated self-hosted runner** (not control-1) when
  you want audited button-click deploys; endgame is GitHub OIDC → Vault for short-lived
  creds. See [CUTOVER.md](CUTOVER.md). **Gates NEXT_STEPS #7.**

## 4. When to apply the WireGuard codification to the live overlay?

It's codified + proven functionally identical, but not applied. The first apply
restarts `wg-quick@wg0` (brief overlay/telemetry blip).
- **Trade-off:** applying makes future runs idempotent no-ops and the on-disk configs
  canonical; not applying leaves the overlay hand-configured (works, but `make deploy`
  won't reconcile it).
- **Recommendation:** apply in a maintenance window over public SSH with provider console
  ready. Low urgency (the live overlay is correct). **Gates NEXT_STEPS #4.**

## 5. Root-token exposure — rotate?

The Vault root token configured the CA (file shredded, value still in `vault-init.json`).
- **Question:** treat it as exposed and rotate, or accept (it stayed on control-1 / your
  laptop)? **Recommendation:** rotate if you want a clean bill; low risk if the machines
  are trusted. **NEXT_STEPS #2.**

## 6. Inventory reproducibility

`inventories/prod/inventory.yml` (host definitions, groups, the `management_network`
group) is **gitignored**, so a fresh clone can't reproduce the inventory — only the
roles/playbooks/committed group_vars.
- **Options:** (a) accept (inventory is operator-local, secrets in SOPS); (b) commit a
  SOPS-encrypted `inventory.sops.yml` and generate `inventory.yml` via `make decrypt`
  (comment-preservation caveat).
- **Recommendation:** revisit if more than one operator needs to deploy from a clean
  clone. Not blocking today.

## 7. Standing decisions to confirm (already acted on, flag if you disagree)

- SSH open to the internet (key-only, no whitelist) fleet-wide — accepted; only
  entry-1 currently runs deeper brute-force history, all now have fail2ban.
- `deploy` user is root-equivalent (docker group) — accepted as named/auditable.
- Roman is a **fleet-wide** operator (incl. control-1/Vault host) — applied.
