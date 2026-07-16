# V7 — customer onboarding, observability access, and closing out V6's firewall fix

V6 got the fleet through its first clean `deploy-e2e` and flagged a firewall
gap (Docker bridge traffic silently relying on already-established
connections) that it fixed on `control-1` but hadn't yet checked on
`entry-1`/`exit-fr`. This round: onboarded the first live customer, opened
observability access, and — prompted by an explicit request to check whether
the same pattern applied elsewhere — found and fixed that `entry-1` had
actually been left in a *worse* state than V6's own troubleshooting realized,
plus a customer-continuity incident caused by fixing it.

## Delivered

- **First live customer onboarded.** Added a runtime user via the Xray API
  (`make api-add`) and generated a `vless://` connection URI plus SOCKS client
  config (`make gen-client`) through `entry-1` → `exit-fr`. Confirmed working.
- **Observability access opened.** Grafana (port 3000 on `control-1`) was
  restricted to a single WireGuard peer; opened it publicly, same tradeoff and
  same operator decision as V6's Prometheus/Loki/Xray-API exposure. Grafana's
  own auth (admin login) is the access control now, same as it would be
  behind WireGuard.

## High — closing the loop on V6's firewall fix

- **`entry-1` never got the `systemctl restart docker` step.** V6 fixed
  `entry-1`'s public port-10085 exposure with a raw `nft -f` reload, at a
  point in that session *before* the Docker-NAT-wipe side effect had been
  discovered (that only surfaced later, on `control-1`). Consequence:
  `entry-1`'s Docker `ip nat` table had been silently absent since V6,
  invisible only because all four of its services use `network_mode: host`
  and never needed Docker-managed NAT. Restarting Docker restored it.
- **`entry-1`'s `forward` chain had no rules at all beyond
  established/related** — not even the wg0-management rule `control-1` has.
  Added the same `ip saddr 172.16.0.0/12 accept` rule applied to `control-1`
  in V6, for consistency and to close the latent gap before anything on that
  host ever needs bridge networking.
- **`exit-fr` audited and confirmed clean.** Its port-10085 fix in V6 used
  `ufw allow`, which never flushes the ruleset — its Docker NAT table and
  forward policy were never at risk. No change needed.

## Medium — incident caused by the fix above, and its remediation

- **Restarting Docker on `entry-1` bounced all four containers** (unlike the
  equivalent restart on `control-1` in V6, which left containers running
  undisturbed) despite both hosts having identical `live-restore: true` in
  `/etc/docker/daemon.json`. The cause of this asymmetry is unresolved — see
  Operator follow-up.
- **The container restart wiped the just-onboarded customer's runtime user**
  (Xray API users are in-memory only and are not persisted across restarts —
  the same behavior already documented and tooled for in `CHANGELOG-V5.md`,
  P0-04). Their already-issued `vless://` link would have started failing.
  Re-added the same UUID/email via the API immediately, restoring service
  without the customer needing a new link.

## Environmental (no code/infra change)

- The operator's local Docker daemon went inactive again mid-session after a
  laptop power-off (`docker.service` is `disabled`, i.e. does not auto-start
  on boot). Restarted manually; see Operator follow-up.
- `entry-1` SSH briefly failed the banner exchange (TCP handshake succeeded,
  but no SSH banner within 8s across repeated attempts) — consistent with
  transient network lag rather than a host problem, confirmed once it
  recovered on its own (`uptime` afterward showed no interruption:
  `load average: 0.35, 0.14, 0.09`, host never went down).

## Validation completed

- `entry-1`: VLESS (443) and Xray API (10085) both externally reachable after
  the Docker restart; `make api-ping` succeeded once local Docker was back.
- `control-1`: Grafana `/api/health` returned `200`; Prometheus (9090), Loki
  (3100), and Grafana (3000) all externally reachable; Docker NAT table and
  all eight platform containers confirmed healthy with undisturbed uptime.
- Customer `customer-20260715-a7a1cd` confirmed present again on `entry-1`
  after re-adding.

## Operator follow-up

1. Investigate why `systemctl restart docker` left `control-1`'s containers
   running but bounced `entry-1`'s, despite identical `live-restore: true`
   config — this determines whether future firewall reloads on `entry-1`
   need to be treated as customer-impacting maintenance windows.
2. `systemctl enable docker` on the operator workstation so a reboot/power
   event doesn't silently take local Docker down again (hit twice now).
3. Adopt `make reconcile` (`scripts/xray-reconcile.sh`, from V5) as standard
   procedure after any restart of `entry-1`/`exit-fr` containers, rather than
   manually tracking and re-adding individual runtime users.
4. `roles/common/templates/nftables.conf.j2` still lacks the general
   Docker-bridge-egress rule flagged in `CHANGELOG_V6.md` — still open,
   tracked separately, not yet applied to the template itself.
