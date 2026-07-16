# VPN fleet: deployable E2E runtime without access hardening

This repository deploys a complete Xray VLESS + REALITY entry/exit fleet, observability,
and a backend-facing Xray gRPC API. The active deployment deliberately does **not** modify
SSH, users, sudoers, nftables, provider firewalls, Fail2ban, security sysctls, Docker daemon
policy, or WireGuard.

## Acceptance command

After filling the production inventory and local secret files, run one of these:

```bash
# Use normal OpenSSH config/agent/default keys
make deploy-e2e 2>&1 | tee deploy-e2e.log

# Force exactly one key (prevents agent key spam / MaxAuthTries failures)
make deploy-e2e SSH_AUTH=key SSH_KEY="$HOME/.ssh/id_ed25519" 2>&1 | tee deploy-e2e.log

# Use one shared root password and disable all public-key attempts
make deploy-e2e SSH_AUTH=password 2>&1 | tee deploy-e2e.log
```

Password mode requires `sshpass` (`sudo apt install sshpass` on Ubuntu). The deployment
performs a non-mutating SSH preflight against every enabled server and aborts before any
server change unless the complete fleet is reachable.

That command performs local validation, deploys the platform, deploys exits, derives exit
REALITY client passwords, wires and deploys entries, exports backend endpoint metadata,
verifies containers/logs/metrics/dashboards/API reachability, then proves the customer path:

```text
Xray API add -> generated client -> entry -> configured default exit -> Internet
             -> per-user stats -> API remove -> fresh connection rejected
```

A successful run ends with `E2E PASS`.

## Current network contract

| Port | Host(s) | Purpose |
|---|---|---|
| 443/TCP | entries and exits | VLESS + REALITY |
| 10085/TCP | entries and exits | Xray gRPC API (public, unauthenticated in this phase) |
| 3000/TCP | platform | Grafana |
| 3100/TCP | platform | Loki ingestion/query |
| 9090/TCP | platform | Prometheus UI and remote-write receiver |
| 9093/TCP | platform | Alertmanager |
| 8200/8201 | platform loopback only | Vault API/cluster |

The public API and telemetry ports are intentionally functional before hardening. Xray's
native API has no built-in authentication in this setup. Do not treat this phase as a final
Internet-exposure policy.

## Backend operations

```bash
UUID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
EMAIL="device-001@example.invalid"

make api-ping NODE=entry-1
make api-add NODE=entry-1 UUID="$UUID" EMAIL="$EMAIL"
make api-has NODE=entry-1 EMAIL="$EMAIL"
make gen-client NODE=entry-1 UUID="$UUID" EMAIL="$EMAIL" OUT=device-001.json
make api-stats NODE=entry-1 PATTERN="$EMAIL"
make api-remove NODE=entry-1 EMAIL="$EMAIL"
```

Runtime API users live in Xray memory and are lost when Xray restarts. The backend must keep
desired state and replay it after deployment/restart. A file-based reconciliation helper is
included:

```bash
make reconcile NODE=entry-1 STATE=.local-secrets/desired-users.json
```

Add `PRUNE=1` only when the state file is authoritative. Add `REPLACE=1` to replace existing
identifiers whose UUID may have changed.

## Required local files

The included production inventory expects:

```text
.local-secrets/grafana-admin-password.txt
.local-secrets/vmshare.ru-fullchain.pem
.local-secrets/vmshare.ru-privkey.pem
```

Create them with mode `0600`. They are intentionally excluded from the archive and Git.
`generated/client-endpoints.json` is created after deployment and also has mode `0600`.

## Controller prerequisites

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install 'ansible-core>=2.18,<2.19' ansible-lint yamllint
make deps
make inventory
make ping
# Or: make ping SSH_AUTH=password
# Or: make ping SSH_AUTH=key SSH_KEY="$HOME/.ssh/id_ed25519"
```

For the final local tunnel test, the controller needs either Docker or a local Xray binary.

## Deployment sequence used by `make deploy-e2e`

1. `make check` — shell/Python/YAML/Ansible/render checks.
2. `playbooks/preflight.yml` — inventory, DNS, certificates, ports, disabled-hardening checks, and all-host SSH/Python reachability before mutation.
3. `playbooks/platform.yml` — Vault and observability.
4. `playbooks/fleet-exits.yml` — active exits.
5. `playbooks/wire-fleet.yml` — derive safe REALITY client passwords and generate entry outbounds.
6. `playbooks/fleet-entries.yml` — active entries and public Xray API.
7. `playbooks/client-metadata.yml` — backend endpoint manifest.
8. `playbooks/verify.yml` — runtime, API, dashboards, Loki and Prometheus checks.
9. `scripts/smoke-all-exits.sh` — add/use/stats/remove/reject E2E proof through every enabled exit.

## Deliberately stubbed

`make management` and `playbooks/management-network.yml` always fail. There is no active
WireGuard role and no variable can silently turn it on. Historical private-network specs are
under `docs/legacy/` and are not instructions.

Read `FIRST_RUN.md`, `API_TESTING.md`, `BACKEND_INTEGRATION.md`, and `TESTING.md` next.
