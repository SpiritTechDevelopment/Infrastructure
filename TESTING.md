# Testing

## Local checks

```bash
make check
```

This checks shell syntax, Python syntax, dashboard JSON, every deployable playbook, and renders
entry/exit Xray configurations. When Docker is available, it also runs Xray's config validator.

## Remote infrastructure verification

```bash
make verify
```

This verifies on every active VPN node:

- Xray, nginx mask, node_exporter and Alloy are running;
- local Xray gRPC API works;
- Xray diagnostics and TLS mask respond;
- public VLESS and API TCP ports are reachable from the controller.

On the platform it verifies Loki, Prometheus, Alertmanager, Grafana, both dashboard UIDs,
Vault process health, metrics from every node, public blackbox probes, and Xray logs from every
VPN node.

## Backend/customer E2E

```bash
make e2e ENTRY=entry-1
```

The test creates a unique runtime user, generates a client from deployed metadata, starts a
local Xray client, confirms the observed public egress equals the entry's configured default
exit, checks per-user stats, removes the user, and proves a fresh tunnel cannot be created.

## All exits

```bash
make e2e-all ENTRY=entry-1
```

This runs the normal unique-user/default-route test and reserved selector-route tests for every other enabled exit.

## Complete acceptance

```bash
make deploy-e2e 2>&1 | tee deploy-e2e.log
```

Do not call a deployment successful if static checks, verification, telemetry, or the final
customer tunnel test fails.
