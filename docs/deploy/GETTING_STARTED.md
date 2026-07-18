# Getting started — run & monitor the fleet

You just cloned the repo and want to operate the fleet. This is the end-to-end path:
**prerequisites → set up → configure → deploy → monitor.**

> This repo is the **configuration control plane**, not a VM provisioner. It assumes
> the servers already exist and you are a **provisioned operator**. VM creation is out
> of scope (Terraform/provider). New operator or new fleet? See
> [OPERATIONS.md](OPERATIONS.md) §4 (onboarding) and
> [ONBOARDING_AND_HARDENING.md](ONBOARDING_AND_HARDENING.md) (new nodes) first.

## 0. What you must already have (the three grants)

A clone alone can't reach or decrypt anything — by design, access is three
independent grants. You need **all three**, held out-of-band:

1. **age private key** (`~/.config/sops/age/keys.txt`) whose public key is a recipient
   in `.sops.yaml` — decrypts the secrets **and the inventory**. *If your key isn't a
   recipient yet, an existing operator must add it and run `sops updatekeys` — you
   cannot self-serve this.*
2. **SSH operator key** listed in the `operators` roster — to reach the hosts.
3. **WireGuard `wg0.conf`** for your peer (`10.20.0.2`) — to reach the overlay, which
   is where all management/telemetry/API/Grafana live.

Without these you can read the code but not run or monitor anything.

## 1. Tooling

```bash
sudo apt install -y age sops wireguard-tools docker.io   # once
python3 -m venv .venv && source .venv/bin/activate
pip install 'ansible-core>=2.18,<2.19' ansible-lint yamllint
ansible-galaxy collection install -r requirements.yml     # (no extra collections; no-op)
```

## 2. Get on the overlay

Everything operational is **overlay-only**. Bring `wg0` up and confirm your address:

```bash
sudo wg-quick up wg0
ip -br addr show wg0        # must show 10.20.0.2/24
ping -c1 10.20.0.1          # hub (control-1) should reply
```

If `wg0` shows no address, `sudo wg-quick down wg0 && sudo wg-quick up wg0`.

## 3. Decrypt — materialize the inventory + secrets

```bash
make decrypt      # sops -> inventory.yml (topology) + secrets.plain.yml (needs your age key)
make deps         # verify prerequisites
make ping         # SSH + Python reachable on every host (reach control-1 via the overlay)
make check        # static validation (what CI runs) — safe, touches nothing live
```

`make decrypt` is what turns a clone into a working checkout: it regenerates the
gitignored `inventory.yml` from `inventory.sops.yml` and the secrets from
`secrets.sops.yml`.

## 4. Configure the system — where everything lives

| You want to change… | Edit | Then |
|---|---|---|
| **Topology** (hosts, which entry uses which exit) | `sops inventories/prod/inventory.sops.yml` | `make decrypt` |
| **Fleet knobs** (images, telemetry hub, Telegram chat/thread, Cloudflare zone/records) | `inventories/prod/group_vars/all.yml` | commit |
| **Secret values** (Grafana pw, TLS, UUIDs, **Telegram bot token**, **Cloudflare token**) | `sops inventories/prod/secrets.sops.yml` | `make decrypt` |
| **Per-host exposure** (public vs overlay-only ports) | `inventories/prod/host_vars/<host>/firewall.yml` | `harden.yml` (dead-man discipline) |
| **DNS** (label → node A-records) | `cloudflare_dns_records` in `group_vars/all.yml` | `make dns` (plan), `make dns APPLY=1` |

The inventory is the **single source of truth**; routing, telemetry, quotas, the
backend roster, and DNS all derive from its labels — see
[../architecture/TOPOLOGY.md](../architecture/TOPOLOGY.md).

## 5. Deploy — and its blast radius

```bash
make deploy                     # whole fleet, idempotent (only changed nodes recreate)
make platform LIMIT=control-1   # platform only — zero data-plane impact
make apply-node LIMIT=entry-1   # one node (bounds the blast radius)
make check-node LIMIT=entry-1   # dry-run a single node first
```

- **Pushing to `main` deploys nothing** — only CI lint runs; deploys are manual.
- A data-plane node is recreated only if **its** config changed → brief reconnect on
  that node (runtime users self-heal in ~30s). See
  [what-deploys-what.md](what-deploys-what.md) for the full command → hosts → roles map.
- After any deploy that restarts `vpn` containers, **reconcile runtime users**:
  `make reconcile NODE=entry-1 STATE=<backend-desired-users.json>`.

## 6. Monitor the system

Join the overlay (step 2), then everything is at the hub:

| Tool | URL (overlay only) | Notes |
|---|---|---|
| **Grafana** | `http://10.20.0.1:3000` | login `admin` / `grafana_admin_password` (from `make decrypt` → `secrets.plain.yml`), or your own account |
| **Prometheus** | `http://10.20.0.1:9090` | the overlay is the gate |
| **Loki** | `http://10.20.0.1:3100` | header `X-Scope-OrgID: ops` |

- **Dashboards** (provisioned): *VPN Fleet Overview* (reachability, CPU/mem/net),
  *VPN Logs*, *VPN Per-User Usage* (top talkers).
- **Alerts → Telegram** — fleet reachability, node/platform telemetry-missing, and
  **Vault seal state** page to Telegram once the bot token/chat are set
  ([OPERATIONS.md](OPERATIONS.md) §6). Grafana also has an *Alertmanager* datasource to
  view firing alerts + silences.
- **Health checks:**
  ```bash
  make verify                 # runtime + API + dashboards + logs + metrics
  make e2e-all ENTRY=entry-1  # provision a throwaway user → connect → confirm egress → cleanup
  make api-ping NODE=entry-1  # Xray API reachable over the overlay
  ```
- **Per-user usage:** `make api-stats NODE=entry-1 [PATTERN=<email>]`, or the usage
  dashboard.

A healthy fleet: all `probe_success` = 1, every enabled node reporting metrics + logs,
and `make e2e-all` passes end-to-end.

## 7. Manage customers (quick reference)

```bash
UUID=$(python3 -c 'import uuid; print(uuid.uuid4())')
make api-add    NODE=entry-1 UUID="$UUID" EMAIL="customer-001"
make gen-client NODE=entry-1 UUID="$UUID" EMAIL="customer-001" OUT=client.json   # prints a vless:// link
make api-remove NODE=entry-1 EMAIL="customer-001"
```

Runtime users are backend-owned and in-memory; the contract is
[../integration/BACKEND_INTEGRATION.md](../integration/BACKEND_INTEGRATION.md).

## Where to next

- **How it's built:** [../architecture/ARCHITECTURE.md](../architecture/ARCHITECTURE.md)
- **What a change touches:** [../architecture/TOPOLOGY.md](../architecture/TOPOLOGY.md)
- **Access/secrets/onboarding in depth:** [OPERATIONS.md](OPERATIONS.md)
- **Security & the overlay:** [../security/README.md](../security/README.md)
- **Where the project stands:** [../status/CURRENT_STATE.md](../status/CURRENT_STATE.md)
