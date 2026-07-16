# V5 — audit remediation

Fixes mapped to the issue register in the static-audit report. Each change was
validated offline (YAML/JSON parse, Jinja render of the touched templates, and
`bash -n` on every script). Live deployment steps that need cluster access are
called out under "Operator follow-up".

## Critical

- **P0-01 — REALITY public-key extraction (VPN did not work).**
  `playbooks/wire-fleet.yml` now parses the public key from its explicit label
  (`Password:` on modern Xray, `Public key:` on older builds) instead of grabbing
  the first 43-char token — which was the *private* key. It asserts the result is
  43 chars and differs from the private key, and the assembly step refuses to write
  wiring unless every active exit produced a valid key. The likely-poisoned
  `inventories/prod/host_vars/entry-1/generated_exits.yml` was **deleted** (untrusted
  artifact); the entry now fails safe to no-exit routing until re-wired.

- **P0-02 — no customer-facing hostname.** Added `public_hostname` / `public_port`
  to `entry-1`, kept separate from the SSH address and the REALITY SNI. The client
  generator now requires a hostname and refuses to emit a bare IP.

- **P0-05 — private key in a world-readable config.** `config.json` is now `0640`
  (not `0644`); the Xray container runs as `10001:0` so it can still read it via the
  root group while other host users cannot. Config dir tightened to `0750`, key dir
  to `0700`. Config validation now runs as root so it never depends on temp-file
  perms. Key logging stays `no_log`.

- **P0-04 — runtime users lost on restart.** Added `scripts/xray-reconcile.sh`
  (+ `make reconcile`): idempotently replays a backend desired-state file into
  HandlerService, with optional `--prune`. This is a backend-triggered tool, not an
  on-node agent (the integration contract deliberately ships no node agent).

## High

- **P1-01 — Xray API helper broken ("can't add users").** `scripts/xray-api.sh` no
  longer duplicates the image entrypoint (`xray xray …` → subcommand directly) and
  makes the request file readable so the non-root container can open the bind-mount.
- **P1-02 — client generator.** `scripts/gen-client.sh` rewritten: reads inventory,
  requires a hostname, derives+validates the REALITY public key (rejects empty /
  malformed / == private), optionally verifies the user via the API, and emits both a
  `vless://` URI and a client JSON.
- **P1-03 — no end-to-end proof.** Added `scripts/smoke-via.sh` (+ `make smoke-via
  ENTRY=… EXIT=…`): provisions a temporary user, connects, compares the observed
  egress IP to the chosen exit, collects stats, cleans up, and fails on any mismatch.
- **P1-04 — offline node blocked deploys.** Added `node_enabled` /
  `management_enabled` lifecycle flags; `management-network.yml` builds an
  `active_management` group on the controller (never contacting offline nodes).
  `exit-nl` is re-declared as `node_enabled: false` instead of being deleted; stale
  inventory `.bak` files removed.
- **P1-07 — Grafana password drift.** Added an idempotent
  `grafana cli admin reset-admin-password` task so the declared password is the
  source of truth on a persistent volume, not just a first-run seed.
- **P1-08 — nginx never reloaded.** Cert, key, config, and page tasks now
  `notify: Restart mask`.
- **P1-09 / malformed docs — deep validation.** `scripts/render-check.sh` and
  `API_TESTING.md` no longer prefix a second `xray` before the subcommand.

## Medium

- **P2-01 — dashboards, logs, alerts.** Added provisioned Grafana dashboards
  ("VPN Fleet Overview" metrics, "VPN Logs" Loki) with stable datasource UIDs and a
  dashboard provider. Added a non-fatal warning when Alertmanager has no receiver.
- Corrected the inventory's misleading "no secret material" comment (service UUIDs
  are sensitive) and cleaned a copy-pasted, duplicated `tmpfs` on the Xray service.
  Removed the stale nested `infra.tar` snapshot.

## Already patched on disk (verified, left as-is)

- **P1-05** nftables no longer flushes Docker tables and permits DNAT'd traffic in
  the forward chain. **P1-06** Alloy config is multiline and its task notifies the
  restart handler.

## Operator follow-up (needs cluster access)

1. Rotate entry+exit REALITY key pairs (a private key was exposed during
   troubleshooting), then `make wire` to regenerate `generated_exits.yml` with the
   corrected parser, and `make apply LIMIT=entry-1`.
2. Create the `entry.vmshare.ru` DNS record → the entry's public address.
3. Run `make smoke-via ENTRY=entry-1 EXIT=exit-fr` and confirm egress.
4. Set a real `alertmanager_webhook_url`.
5. Decide the per-customer routing model (P0-03) before issuing production
   multi-hop profiles — the shared `via-fr` selector is throwaway-only.
