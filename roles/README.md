# Role map

| Role | Current responsibility |
|---|---|
| `common` | base runtime packages, chrony and timezone only; no users/access/hardening |
| `docker` | install/start Docker and confirm Compose v2; no daemon configuration |
| `xray` | VLESS/REALITY config, public API, stats, logs and retained REALITY key |
| `nginx_mask` | local TLS mask destination for REALITY |
| `node_exporter` | documents node-exporter ownership in the VPN Compose stack |
| `alloy` | VPN Docker logs to Loki and node metrics to Prometheus remote-write |
| `vpn_stack` | applies the complete VPN Compose stack after all configs exist |
| `vault` | localhost-bound Vault process; initialization remains manual |
| `observability` | Loki, Prometheus, Alertmanager, Grafana, blackbox probes and telemetry |

There is no active WireGuard role. `playbooks/management-network.yml` is a deliberate
failure stub. Backend customer operations use Xray HandlerService and StatsService
directly; no custom node agent is installed.
