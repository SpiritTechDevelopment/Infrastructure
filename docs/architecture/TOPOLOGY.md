# Topology — the config-driven fleet model

The **inventory is the single source of truth.** Every entity is a label
(`control-1`, `entry-1`, `exit-fr`), every entry declares which exit(s) it routes
to by **tag** (`fr-exit`, `nl-exit`), and every consumer — data plane, telemetry,
quotas, the backend roster, DNS — **derives from those labels**. Changing topology
is a config edit; nothing is wired by hand.

> The desired state is declarative today; the *automatic apply* (a control-node
> reconcile loop) is deferred until there's a dedicated runner box on the overlay
> ([CUTOVER.md](../deploy/CUTOVER.md) / NEXT_STEPS #7). Until then the trigger is `make deploy`
> (node-scoped). The model below holds regardless of the trigger.

## Everything derives from the inventory

| Consumer | Derives from | File |
|---|---|---|
| Xray routing (entry→exit) | `exit.tag` / `entry_default_exit_tag` | `roles/xray/templates/config.json.j2` |
| Quotas / per-user usage | `groups['entry']` | `roles/observability/templates/usage-exporter-targets.yml.j2` |
| Backend endpoint roster | inventory → manifest | `playbooks/client-metadata.yml` → `generated/client-endpoints.json` |
| Grafana / logs | push telemetry, labeled by `inventory_hostname` / `country` | `roles/alloy` |
| Reachability probes + alerts | `groups['entry']+['exit']` | `roles/observability/templates/fleet-targets.yml.j2` |
| **DNS** | `cloudflare_dns_records` (label → node → public IP) | `roles/cloudflare_dns` |

## The guarantee: the backend never feels exit topology

The backend manages users on the **entry** (the identity point) and quotas read
per-user stats from the **entry**. The exit a customer egresses through is just a
routing decision *inside* the entry's Xray — invisible to the backend and to
accounting. So **re-routing an entry to a different exit changes nothing for the
backend or quotas**: same users, same entry, same stats, different egress country.

## What a topology change touches

| Change | Backend / quotas | Grafana / logs | Data plane |
|---|---|---|---|
| Re-route entry → other exit | **unaffected** | new egress-country label | that entry's xray recreates → brief blip on *its* customers (self-heal re-adds users) |
| Add an **entry** | manifest updates → backend tracks it | auto-appears (push) | isolated to that node |
| Remove an **entry** | manifest updates → backend stops using it | series stops (history retained) | its customers move via DNS |
| Add an **exit** | unaffected until an entry routes to it | appears when enabled | does not touch entries until wired |
| Re-*label* a node | new metric/log series under the new label | history discontinuity | — |

Non-disruption rests on two rules: **deploy node-scoped** (`--limit` / `apply-node`,
so unchanged nodes aren't recreated) and **review disruptive changes** (adding an
exit rewires entries). The WireGuard re-render is fleet-wide but is *management*
plane — customer traffic is `entry:443 → exit:443` over the public internet, so it
is data-plane-safe.

## Add / remove / replace as config

- **Add an entry:** declare it in the encrypted inventory (`entry` +
  `management_network` groups, its own `xray_api_overlay_host`) + a
  `host_vars/<h>/firewall.yml`; bootstrap trust once (a brand-new machine must first
  trust the control side — see [ONBOARDING_AND_HARDENING.md](../deploy/ONBOARDING_AND_HARDENING.md)
  §4); deploy node-scoped; add a DNS record.
- **Replace (drop-in):** point the DNS record at the new node and give the new node
  the old REALITY identity (pin `reality_private_key`, keep `reality_short_ids` /
  `reality_server_names`) so existing client profiles keep working; the new node
  comes up before cutover, then `make dns APPLY=1` flips the record, then decommission
  the old. See OPERATIONS §5.
- **Remove:** drop it from the inventory (or `node_enabled: false`), remove its WG
  peer, re-render the overlay.

## DNS reconciliation (`make dns`)

DNS follows the topology. Declare records in `group_vars/all.yml`:

```yaml
cloudflare_zone: "example.com"
cloudflare_dns_records:
  - { name: "entry.example.com", node: entry-1 }   # A -> entry-1 public IP, DNS-only
  - { name: "fr.example.com",    node: exit-fr }
```

`node:` resolves to that node's public IP (`public_ip | default(ansible_host)`).
`proxied` defaults **false** — REALITY on `:443` must be DNS-only (a Cloudflare
proxy would terminate TLS and break the data plane).

```bash
make dns            # PLAN (dry-run) — prints CREATE/UPDATE/ok per record
make dns APPLY=1    # apply the changes
```

Token: `cloudflare_api_token` (SOPS, shared with the `acme` role). The reconciler is
`scripts/cloudflare-dns-sync.py` (stdlib only; plan-by-default). Reconciliation
no-ops unless zone + records + token are all set.
