# Repository audit and corrections

> **⚠ Historical audit (point-in-time).** This snapshot predates the hardening
> convergence. Its "Xray API and telemetry are public / unauthenticated" findings
> are **resolved**: those surfaces are now overlay-only and the firewall is
> codified fleet-wide. For current state see [CONVERGENCE_STATUS.md](CONVERGENCE_STATUS.md)
> and [ARCHITECTURE.md](ARCHITECTURE.md).

## Failure pattern removed

The prior design coupled fixed SSH source allowlists, root/login restrictions, a
default-drop firewall and a mandatory WireGuard path. The same private path carried
administration, Xray API, logs, metrics and dashboards. A tunnel failure could remove
both repair access and diagnostic evidence.

## Access and deployment boundaries

- Normal deployment never creates users, installs authorized keys, edits sudoers/sshd,
  removes sshd files, manages nftables/fail2ban/sysctls/auditd, configures WireGuard or
  writes Docker daemon policy.
- All access/hardening flags are false and asserted false in preflight and deploy plays.
- The management-network playbook always fails and cannot be enabled by variables.
- Runtime containers use a functional, unrestricted Compose baseline during this phase.
- Vault remains localhost-bound; the Xray API and telemetry path are public by explicit
  functional-phase choice.

## First-deploy lifecycle fix

The old Xray, nginx and Alloy roles notified service handlers before `/opt/vpn/compose.yml`
necessarily existed. On a fresh host, handlers could run in role order and fail before
the final `vpn_stack` role rendered Compose.

Configuration roles now only render/register changes. `vpn_stack` applies Compose once,
after every config, certificate and directory exists, and force-recreates services only
when inputs changed.

## Xray, API and routing

- HandlerService, StatsService and ReflectionService are enabled on every active node.
- API commands use deadlines and exact JSON-aware user identifier matching rather than
  substring grep.
- Add/remove operations verify exact postconditions.
- The reconciler parses actual users exactly, validates desired UUIDs/identifiers and
  protects infrastructure identities during pruning.
- Entry outbounds are generated after exits deploy. REALITY client passwords are derived
  with the pinned Xray image, validated and rejected if equal to the private key.
- Normal unique API-created users route through `entry_default_exit_tag`.
- Current client JSON uses `realitySettings.password`; generated VLESS URIs retain the
  conventional `pbk` parameter.
- Runtime customer users remain process-memory state; the backend must reconcile them.

## Networking and observability

- Fleet logs and node metrics use the public control-plane address and do not depend on
  WireGuard.
- Xray/nginx emit container logs; Alloy labels and ships them to Loki.
- Prometheus receives fleet node metrics and scrapes platform metrics.
- Blackbox Exporter probes both customer TCP/443 and Xray API TCP/10085 per node, with
  node/service labels and alerts.
- Grafana dashboards show node health plus customer/API reachability and are verified by
  stable UID.
- Xray `/debug/vars` is treated as localhost diagnostics, while per-user traffic is
  verified through StatsService.

## End-to-end acceptance

`make deploy-e2e` validates inventory, deploys in dependency order, verifies runtime and
telemetry, adds a unique user through the public API, creates a client, observes traffic
through the configured exit, checks counters, removes the user and ensures no fresh
tunnel succeeds for the entire rejection window.

## Deliberate limitations

The Xray API and telemetry ingestion are unauthenticated in this phase. Vault is deployed
but not auto-initialized and is not part of customer provisioning. Provider/host firewall
rules are external prerequisites. These are explicit limitations rather than hidden
private-network dependencies.
