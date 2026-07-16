# Logging policy

Current runtime logging is enabled so the fleet is diagnosable and the deployment can
prove log delivery end to end.

Collected into Loki tenant `ops`:

- Xray access and error output from container stdout/stderr;
- nginx mask access/error output;
- Docker service lifecycle/container output for the VPN and platform Compose projects;
- control-plane service logs.

Xray StatsService separately exposes aggregate uplink/downlink counters keyed by the
backend-supplied unique accounting identifier.

The current Loki `ops` retention is 30 days. Access logs can contain network identifiers
and connection metadata. Do not log client configuration payloads, UUID/private keys,
Vault tokens, API request files, REALITY private keys or backend secrets. The separate
`activity` tenant remains unused.
