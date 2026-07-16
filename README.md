# Spirit VPN — Infrastructure

Ansible-managed VPN fleet: an Xray **VLESS + REALITY** entry/exit data plane, a
platform host (Vault + observability), a backend-facing Xray gRPC API, and the
host hardening / overlay-first exposure that keeps management off the public
internet.

> **Mental model — three isolated planes:**
> - **Data plane** (customers): `Customer → entry-1:443 (REALITY) → exit:443 → Internet`. Public `:443` only. Never break it.
> - **Management plane** (operators/CI): SSH + the Xray API — over the **WireGuard overlay** (`10.20.0.0/24`).
> - **Telemetry plane**: nodes push metrics/logs to the platform host over the overlay.
>
> Only **`:443` (data)** and **`:22`/`:232` (key-only SSH)** are public. Everything
> else (Xray API `10085`, Grafana `3000`, Prometheus `9090`, Loki `3100`, Vault,
> Alertmanager) is reachable **only over the overlay**.

## Fleet

| Host | Role | Public IP | SSH | Overlay |
|---|---|---|---|---|
| `control-1` | platform (Vault + observability), WG **hub** | 193.247.81.167 | :22 | 10.20.0.1 |
| `entry-1` | VPN entry | 5.101.67.252 | :232 | 10.20.0.11 |
| `exit-fr` | VPN exit (France) | 151.247.196.239 | :22 | 10.20.0.21 |
| `exit-nl` | VPN exit (Netherlands) | — | — | disabled |

## Quickstart

Deploys run from an operator workstation that is a **WireGuard overlay peer** (see
[OPERATIONS.md](OPERATIONS.md) for onboarding + how access is granted).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install 'ansible-core>=2.18,<2.19' ansible-lint yamllint
ansible-galaxy collection install -r requirements.yml
sudo wg-quick up wg0          # join the management overlay
make decrypt                  # materialize SOPS secrets -> secrets.plain.yml (needs your age key)
make check                    # static validation (also what CI runs)
make deploy                   # full deploy + verify  (see the discipline below)
```

> **Discipline (read before deploying):**
> - A full `make deploy` can restart the `vpn` containers, which **wipes in-memory
>   runtime users** — always `make reconcile NODE=entry-1 STATE=…` afterward.
> - To change **operator SSH access only**, use the scoped, non-disruptive
>   `playbooks/access.yml` (never a full deploy) — `--check --diff` first.
> - Firewall/access changes: apply with a dead-man auto-revert and verify before
>   committing. See [CONVERGENCE_STATUS.md](CONVERGENCE_STATUS.md) §5.

## Documentation

| Doc | What |
|---|---|
| [OPERATIONS.md](OPERATIONS.md) | **Start here** — access model (repo/secrets/overlay), Git workflow, onboarding an operator, deploying, monitoring |
| [ARCHITECTURE.md](ARCHITECTURE.md) | System design, the three planes, container networking, runbook |
| [RECOVERY.md](RECOVERY.md) | Passphrase-encrypted recovery bundles — surviving a lost laptop |
| [CUTOVER.md](CUTOVER.md) | Moving CI from a hosted runner to a self-hosted one on the overlay |
| [CONVERGENCE_STATUS.md](CONVERGENCE_STATUS.md) | Hardening convergence: state, decisions, what's next, gotchas |
| [ONBOARDING_AND_HARDENING.md](ONBOARDING_AND_HARDENING.md) | Node onboarding + the host-hardening plan |
| [BACKEND_INTEGRATION.md](BACKEND_INTEGRATION.md) | Runtime-user contract for the backend |

## Secrets

Deploy secrets (Grafana password, TLS cert/key, `entry_service_uuid`) live
**SOPS-encrypted in the repo** (`inventories/prod/secrets.sops.yml`) and are
materialized by `make decrypt` (→ gitignored `secrets.plain.yml`, passed to
Ansible as extra-vars). Private keys, Vault unseal keys, and `.local-secrets/`
never enter Git. Full model + onboarding in [OPERATIONS.md](OPERATIONS.md) §3.

## Backend / customer operations

```bash
UUID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
EMAIL="device-001@example.invalid"
make api-add    NODE=entry-1 UUID="$UUID" EMAIL="$EMAIL"
make gen-client NODE=entry-1 UUID="$UUID" EMAIL="$EMAIL" OUT=device-001.json
make api-stats  NODE=entry-1 PATTERN="$EMAIL"
make api-remove NODE=entry-1 EMAIL="$EMAIL"
```

Runtime API users live in Xray memory and are lost on restart; the backend keeps
desired state and replays it (`make reconcile`). Contract: [BACKEND_INTEGRATION.md](BACKEND_INTEGRATION.md).

## Verify

```bash
make e2e-all ENTRY=entry-1     # provision throwaway user → connect → confirm egress → cleanup
make api-ping NODE=entry-1     # Xray API reachable over the overlay
make verify                    # runtime + API + dashboards + logs + metrics
```

A healthy fleet ends E2E with `E2E PASS`.

## CI

`.github/workflows/ci.yml` runs lint/static checks on every PR (hosted runner, no
secrets). `.github/workflows/deploy.yml` is an interim hosted-runner deploy;
production deploys currently run from the workstation. See [CUTOVER.md](CUTOVER.md).
