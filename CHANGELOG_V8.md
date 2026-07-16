# V8 — per-user usage visibility (top-talkers dashboard)

V8 adds operator visibility into per-customer traffic so abusers can be spotted
and manually cut (`make api-remove`), without waiting for the not-yet-built
backend that will own durable quota accounting. This is deliberately the
smallest useful-today slice of the larger quota control-loop that was
discussed and scoped but intentionally *not* built here: **infra observes and
signals; the backend enforces and stores**, per `BACKEND_INTEGRATION.md` and
`governance/data-catalog.yml` (which already assign durable per-user usage
retention to "per backend billing policy").

Deployed and verified live on `control-1` + `entry-1`.

## Delivered

- **`xray-usage-exporter`** — a new component in the observability Compose
  project on `control-1`. It reads Xray StatsService per-user byte counters
  from each entry node and re-exposes them as Prometheus metrics
  (`xray_user_traffic_bytes_total{node,email,direction}`).
  - **Non-destructive:** never uses `statsquery -reset`, so it does not
    disturb the counters any future backend accounting loop will rely on.
  - **No `docker.sock`:** the pinned, statically-linked Xray client binary is
    baked into a `python:3-slim` image via a multi-stage build
    (`COPY --from={{ xray_image }}`), so the exporter speaks to StatsService
    as a network client without the socket-mount privilege the poller
    alternative would have needed.
  - **Cardinality-bounded:** `usage_exporter_top_n` (default 50) caps the
    number of per-user series exported, so this never becomes the
    per-user-Prometheus cardinality anti-pattern at scale.
- **"VPN Per-User Usage" Grafana dashboard** (uid `vpn-user-usage`): top
  users by total bytes (table), top users by live throughput (timeseries),
  aggregate customer throughput by direction, and active-user/traffic stats.
- **Data source is StatsService, not access logs** — a deliberate choice:
  prod runs with `xray_access_log: ""` (logging off), and enabling access
  logs to mine per-user activity would add PII-medium connection metadata
  (`governance/logging-policy.md`) and still lack byte totals. StatsService
  gives exact per-user bytes keyed only by the pseudonymous accounting id.

## Fixed (gap found during implementation)

- **Prometheus never reloaded on `scrape_configs` change.** Adding the
  `xray-usage` scrape job to the bind-mounted `prometheus.yml` had no effect,
  because Prometheus reads `scrape_configs` only at startup and Compose does
  not recreate it when only a bind-mounted file changes. Added a
  `Restart Prometheus` handler (`roles/observability/handlers/main.yml`,
  previously empty) notified from the config render task. This was a latent
  gap — no prior deploy had added a scrape job after first boot, so it had
  never been exercised. (`file_sd` targets like `fleet-targets.yml` are
  auto-reloaded and were unaffected; only `prometheus.yml`/rules changes need
  the restart.)

## Governance

- `governance/data-catalog.yml`'s `per_user_usage` entry now documents the new
  `operational_exposure`: pseudonymous accounting id + byte counters exposed
  as Prometheus metrics at tsdb retention, explicitly marked as **not** the
  authoritative billing store (that remains the backend's, per the existing
  retention line).

## Validation completed

- `make check` — dashboard JSON validates, syntax/render pass.
- Local end-to-end pre-flight: built the exporter image and ran it against
  `entry-1`'s live StatsService before deploying — confirmed correct metrics
  output and healthcheck.
- `make platform` — built and started the exporter on `control-1`; clean
  `PLAY RECAP` (0 failed).
- Post-deploy: `xray-usage` Prometheus target `up`; drove ~9 MB through the
  live customer tunnel (egress confirmed via the exit) and watched
  `xray_user_traffic_bytes_total` climb from 1,227 to 9,035,494 bytes; the
  dashboard's `topk` query ranks the customer correctly.

## Known minor wart

- The observability apply now runs `docker compose up ... --build`, which can
  report "Built" on every run (cosmetic `changed: true`). Chosen for
  correctness (the exporter image is always rebuilt from current source); a
  `community.docker` module would give true build idempotency but adds a
  collection dependency the repo deliberately avoids.

## Explicitly out of scope (deferred, by decision)

- Durable usage/quota storage (backend owns it — no Postgres here).
- Automated enforcement / user removal (backend acts on the signal).
- Alertmanager quota rules + webhook wiring (inert until a backend consumes
  them; build when that backend is being built).
- Per-user or aggregate traffic shaping (kernel-default `fq` already gives
  per-flow fairness).
