# What deploys what

The exact map from **command → playbook → hosts → roles → effect**, so you know the
blast radius before you run anything. Source of truth is the encrypted inventory
(`inventory.sops.yml`); reach `control-1` over the overlay (`-e ansible_host=10.20.0.1`).

## The one-command paths (`make`)

| Command | Playbook | Hosts | What it does |
|---|---|---|---|
| `make deploy` | `site.yml` | whole fleet | preflight → platform → exits → wire → entries → client-metadata → verify (idempotent; only changed nodes recreate) |
| `make deploy-e2e` | + static checks + E2E | whole fleet | `make deploy` bracketed by `make check` and `make e2e-all` |
| `make platform` | `platform.yml` | `control-1` | Vault + observability (no data-plane impact) |
| `make backend-staging EXTRA_VARS=/secure/backend.yml` | `backend-staging.yml` | `control-1` | Immutable SpiritVPN backend image + isolated PostgreSQL staging stack |
| `make apply-node LIMIT=<h>` | `fleet-infra.yml` (limited) | one node | redeploy a single data-plane node (must be wired) |
| `make wire` | `wire-fleet.yml` | localhost | rebuild entries' `entry_exits` from deployed exits' REALITY material |
| `make management` | `management-network.yml` | `management_network` (all) | WireGuard overlay (`wg0`) — **restarts wg0**, do in a window |
| `make certs` | `acme.yml` | `control-1` (default) | ACME/certbot cert issuance (Cloudflare DNS-01) |
| `make dns` | `dns.yml` | localhost | reconcile Cloudflare DNS from inventory labels (plan; `APPLY=1` to apply) |
| `make reconcile NODE= STATE=` | `scripts/xray-reconcile.sh` | one entry's API | replay backend desired runtime users |
| `make verify` | `verify.yml` | fleet | runtime + API + dashboards + logs + metrics |

Hardening/access are **separate** from the routine deploy (deliberately):

| Command / playbook | Hosts | What it does |
|---|---|---|
| `playbooks/harden.yml` (`-e deploy_mode=hardened …`) | target | firewall / sshd / fail2ban / sysctl / auditd / unattended-upgrades (dead-man discipline) |
| `playbooks/access.yml` | fleet | render operator `authorized_keys` (scoped, non-disruptive) |

## `site.yml` order (what `make deploy` runs)

`preflight` → `platform.yml` → `fleet-exits.yml` → `wire-fleet.yml` →
`fleet-entries.yml` → `client-metadata.yml` → `verify.yml`. Exits deploy *before*
entries because `wire` builds entry outbounds from the exits' REALITY material.

## Which roles run where

| Playbook | Host group | Roles (in order) |
|---|---|---|
| `platform.yml` | `platform` (control-1) | common, docker, **vault**, **observability** |
| `fleet-exits.yml` | exits | common, docker, **xray**, nginx_mask, node_exporter, alloy, **vpn_stack** |
| `fleet-entries.yml` | entries | common, docker, **xray**, nginx_mask, node_exporter, alloy, **vpn_stack** |
| `fleet-infra.yml` | all data-plane nodes | (same 7 as entries/exits) — used by `apply-node` |
| `management-network.yml` | `management_network` | **management_wireguard** |
| `acme.yml` | control-1 | **acme** |
| `dns.yml` | localhost | **cloudflare_dns** |

## What each role deploys

| Role | Deploys | On |
|---|---|---|
| `common` | base packages, `deploy` user, and (when `deploy_mode≠runtime`) sshd/nftables/fail2ban/sysctl/auditd/unattended-upgrades + Vault-CA trust | all hosts |
| `docker` | Docker Engine + Compose v2 | all hosts |
| `vault` | Vault container (Raft, loopback-only) + SSH CA + seal-metric timer | control-1 |
| `observability` | Prometheus/Loki/Grafana/Alertmanager/blackbox/node-exporter/alloy/usage-exporter + rules + Telegram | control-1 |
| `xray` | `config.json` (VLESS/REALITY inbound, exit outbounds, routing, API, stats) | entries/exits |
| `nginx_mask` | masking site + TLS | entries/exits |
| `node_exporter` / `alloy` | host metrics + log/metric push to the hub | entries/exits |
| `vpn_stack` | the `vpn` Compose project + the auto-reconcile timer (entries) | entries/exits |
| `management_wireguard` | `wg0` overlay config | all overlay hosts |
| `acme` | certbot + Cloudflare DNS-01 | control-1 |
| `cloudflare_dns` | label-driven DNS records (via the Cloudflare API) | localhost |

## Blast radius, at a glance

- **`make platform`** — control-1 only; **zero data-plane impact** (a Vault reseal is
  the only caveat, if the Vault container is recreated).
- **A data-plane node redeploy** — recreates that node's `vpn` containers only if its
  rendered config actually changed → brief reconnect on *that* node (runtime users
  self-heal in ~30s). Use `apply-node LIMIT=` to scope it.
- **`make deploy`** — re-renders the whole fleet but only recreates changed nodes.
- **`make management`** — restarts `wg0` fleet-wide (management plane; data plane is
  unaffected — customer traffic doesn't traverse the overlay).
- **Pushing to `main`** — deploys **nothing** (only CI lint runs); deploys are manual.

See [ARCHITECTURE.md](../architecture/ARCHITECTURE.md) for the layered view and
[TOPOLOGY.md](../architecture/TOPOLOGY.md) for how a config change propagates.
