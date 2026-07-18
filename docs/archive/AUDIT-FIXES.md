# Active remediation summary

The prior design coupled SSH access, firewall policy, private WireGuard reachability, Xray API,
logs, metrics and dashboards. A management-tunnel failure therefore removed both access and
observability.

The active implementation separates functional deployment from access policy:

- all SSH/user/firewall/Fail2ban/WireGuard code is removed or hard-stubbed;
- Xray API is enabled with HandlerService, StatsService and ReflectionService;
- normal API-created users use the configured default exit;
- exit REALITY client passwords are derived from deployed private keys and validated before
  generated wiring is written;
- logs and metrics use the public control-plane address;
- platform, fleet, metadata, verification and a real add/tunnel/stats/remove E2E test are part
  of one `make deploy-e2e` command;
- stale generated wiring and local secret material are excluded from the distribution.

Historical specifications are retained under `docs/legacy/` only.
