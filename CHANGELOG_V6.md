# V6 — first clean end-to-end live deployment

V5 fixed everything that offline/static validation (YAML parse, Jinja render,
`bash -n`) could catch. V6 fixes what only surfaced by actually running
`make deploy-e2e` against the real fleet (`control-1`, `entry-1`, `exit-fr`) for
the first time: a handful of remaining Ansible bugs, plus firewall/runtime drift
on the live hosts that predates this repo's current "no-hardening phase" and
was never exercised until now. The run now completes clean through
`make deploy-e2e` and `make e2e-all`, including a full customer-flow smoke test
(API-provisioned user → connect via `entry-1` → egress through `exit-fr` →
verify egress IP → stats → remove user → confirm rejected).

## Critical

- **Local render play attempted `sudo` on the operator's workstation.**
  `playbooks/render-check.yml`'s second play (`connection: local`) inherited
  `ansible_become: true` from the inventory, which is a connection *variable*
  and — contrary to the intuitive reading of play-level keywords — takes
  precedence over a play's own `become: false`. Fixed by also setting
  `ansible_become: false` in the play's `vars:`, the pattern already established
  by hotfix 1 elsewhere in the repo but missed here.

- **Entry REALITY client-password extraction silently produced an empty
  string.** `playbooks/client-metadata.yml` used `regex_findall('...\\s...')`
  inside a YAML folded (`>-`) scalar. Folded/plain YAML scalars do not process
  backslash escapes, so `\\s` survives as two literal backslash characters;
  once Jinja's own string-literal lexer processes that, the resulting pattern
  no longer matches whitespace, so the regex silently returned nothing and the
  `length == 43` assertion failed. This is the exact bug class fixed once
  already in `roles/xray/tasks/main.yml` (CHANGELOG-E2E-HOTFIX-4) and
  correctly avoided in `playbooks/wire-fleet.yml`'s exit-side derivation — but
  it was reintroduced here, in a newer file, with no regression-test coverage
  (`playbooks/reality-key-parser-test.yml` only exercises the exit-side
  parser). Fixed by reusing the already-correct, previously-unused
  `_reality_public_key_output_regex` play variable that was sitting right next
  to the broken inline pattern.

- **`inventory_dir` is undefined for the implicit `localhost` host.**
  `playbooks/wire-fleet.yml` wrote generated per-entry wiring to
  `{{ inventory_dir }}/host_vars/...`. `inventory_dir` is only populated for
  hosts that were actually sourced from an inventory file; the plays in this
  file run on the *implicit* `localhost` (not declared in
  `inventories/prod/inventory.yml`), so the variable was always undefined and
  the task failed every time wiring needed to persist. Fixed by deriving the
  inventory directory from `ansible_inventory_sources` instead, which is
  populated for every host regardless of how it entered the run.

- **`loki_port` / `vault_api_port` (and siblings) undefined in the platform
  health-check play.** `playbooks/verify.yml`'s "Verify platform health..."
  play referenced `loki_port`, `prometheus_port`, `alertmanager_port`,
  `grafana_port`, and `vault_api_port` as bare variables. These are role
  *default* variables (from `roles/observability/defaults/main.yml` and
  `roles/vault/defaults/main.yml`); role defaults are only in scope for plays
  that actually include that role, and do not persist into a later, separate
  play even within the same `ansible-playbook` run (verified empirically).
  Fixed with `vars_files` pointing at both role defaults files, so the port
  numbers stay sourced from one place instead of being hand-duplicated.

- **`xray` container could not read its own config.** `config.json` is
  rendered `root:root 0640` (per V5's P0-05 fix), but
  `roles/vpn_stack/templates/compose.yml.j2` never pinned a `user:` for the
  `xray` service, so it ran as the image's default non-root UID (`65532`) and
  got `permission denied` on every start — a crash loop. The render task's own
  `validate:` command already worked around this by forcing `--user 0:0`,
  which masked the mismatch at render time. Fixed by adding `user: "0:0"` to
  the compose service definition to match what was already being validated.

## High — live-host firewall drift (not code; applied directly to the fleet)

All three hosts (`control-1`, `entry-1`, `exit-fr`) already carry firewalls
that were **not** applied by this repo (`common_manage_firewall: false`
everywhere) and are stricter than this repo's documented "no-hardening phase"
assumes:

- `entry-1` / `exit-fr`: the Xray gRPC API (10085) was WireGuard-management-only.
  `verify.yml` expects it reachable from the deployment controller over the
  public internet (`xray_api_public_mode: true`). Opened 10085 publicly on
  both hosts (nftables on `entry-1`, `ufw` on `exit-fr`) — operator decision,
  consistent with the documented no-hardening design.
- `control-1`: Prometheus (9090) and Loki (3100) were WireGuard-only, but
  `entry-1`/`exit-fr` push metrics/logs to `control-1`'s **public** IP
  (`prometheus_remote_write`, `loki_ops_endpoint`). Opened both ports publicly
  on `control-1`, same rationale.
- `entry-1` / `exit-fr` also each had a **leftover native service** predating
  this deploy — a bare-metal `nginx` and a bare-metal `xray.service` — bound to
  the same ports the Dockerized stack needed (`127.0.0.1:8443` and `:443`
  respectively), crash-looping the containerized equivalents. Stopped and
  disabled both.

## Medium — live-host firewall bugs surfaced by the above (fixed on the host)

Applying the `control-1` firewall change the naive way (`nft -f` a full
ruleset, which starts with `flush ruleset`) exposed two more bugs in the
*existing, manually-maintained* firewall — not introduced by this session, but
never exercised before because the affected connections had already been
established prior to any of this troubleshooting:

- **`flush ruleset` also wiped Docker's own NAT table.** Every Docker
  port-published service on `control-1` (Prometheus, Loki, Grafana, Vault,
  Alertmanager) broke immediately, including over WireGuard, since Docker's
  `iptables-nft`-managed DNAT rules live in a separate table that a blanket
  flush also deletes and does not automatically reconcile. Fixed with
  `systemctl restart docker`, which reprograms its network rules without
  restarting any container (confirmed via unchanged container uptimes and
  resumed traffic counters).
- **The `forward` chain never actually allowed container-to-container bridge
  traffic, nor bridge-to-internet egress.** Once the restart above reset
  conntrack state, Prometheus could no longer scrape its own `node-exporter`
  container and `blackbox-exporter` could no longer reach the public VLESS/API
  ports it probes — both were silently relying on connections that were
  already `established` before the flush. Fixed by adding
  `ip saddr 172.16.0.0/12 accept` to the `forward` chain (Docker's private
  bridge range), covering both intra-bridge traffic and general egress.

  **Note:** `roles/common/templates/nftables.conf.j2` — the Ansible-managed
  firewall this repo *would* apply if `common_manage_firewall` were turned on
  — has the same gap (no general Docker-bridge-egress rule in its `forward`
  chain). It was not touched this session since it isn't currently in use on
  any of the three hosts, but it will reproduce this exact outage the day
  firewall management is turned on. See `CHANGELOG-REVIEW-V6.md` for the
  recommended follow-up.

## Environmental (no code change)

- `entry-1`'s first-ever pull of `ghcr.io/xtls/xray-core:26.3.27` happened
  inline inside Ansible's `validate:` step (which runs on the target host, not
  the controller) and intermittently failed under the combined weight of a
  cold ~20-layer image pull plus `no_log: true` hiding the real error. Warming
  the image cache ahead of the run resolved it; no code was at fault.

## Validation completed

- `make check` (syntax, offline render, deep `xray -test` validation): passed.
- `make deploy-e2e` against `inventories/prod/inventory.yml`: passed —
  `control-1`, `entry-1`, `exit-fr` all fully deployed and verified (Vault
  health, Prometheus/Loki readiness and cross-fleet metric/log presence,
  Grafana dashboards, every public VLESS and Xray API endpoint reachable).
- `make e2e-all ENTRY=entry-1`: passed — API-provisioned throwaway user,
  connected through `entry-1`, egress verified as `exit-fr`'s public IP,
  per-user stats collected, user removed, fresh connection confirmed rejected.

## Operator follow-up

1. Decide whether `common_manage_firewall` should be turned on so this repo
   becomes the source of truth for the firewalls that are currently
   hand-maintained on all three hosts (drift risk: the live rules and
   `roles/common/templates/nftables.conf.j2` have already diverged once this
   session, in both directions).
2. Before turning `common_manage_firewall` on: add a general Docker-bridge
   egress/forward rule to `roles/common/templates/nftables.conf.j2` — see the
   note above.
3. The public exposure of Xray API (10085), Prometheus (9090), and Loki (3100)
   on `entry-1`/`exit-fr`/`control-1` was an explicit operator decision to
   match the documented no-hardening phase; revisit before any real hardening
   pass.
4. Add regression coverage for the entry-side REALITY password parser in
   `playbooks/reality-key-parser-test.yml` (it currently only covers the
   exit-side parser), so this bug class can't reappear a third time.
