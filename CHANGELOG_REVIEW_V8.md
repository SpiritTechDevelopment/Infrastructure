# V8 review — implementation problems found and solutions applied

Chronological account of building the per-user usage panel: the design forks
that were resolved, the traps avoided, and the one real gap found and fixed.
See `CHANGELOG_V8.md` for the summarized version.

---

### 1. Data source: StatsService vs. access logs

**Question:** where do per-user byte totals come from?

**Investigated:** prod has `xray_access_log: ""` (access logging off). Enabling
it to mine per-user activity would (a) add PII-medium connection metadata per
`governance/logging-policy.md` / `data-catalog.yml`, and (b) still not provide
byte *totals* — access logs carry connection events, not counters.

**Decision:** use Xray StatsService (`statsquery -pattern "user>>>"`), which
gives exact per-user up/down byte counters keyed only by the pseudonymous
accounting id — already sanctioned by `data-catalog.yml`'s `per_user_usage`
entry, with no logging/privacy change. Non-destructive (never `-reset`), so it
does not disturb the counters a future backend accounting loop will read.

---

### 2. Variable-scope trap (avoided, not hit)

**Concern:** the exporter's Dockerfile needs the Xray image reference. On the
platform play (`control-1`), was `xray_image` actually in scope, or only an
`xray`-role default? This is the exact class of bug that bit `loki_port` in V6.

**Checked before writing:** `xray_image` is defined in
`inventories/prod/group_vars/all.yml` (global), and confirmed empirically
(`ansible -m debug -a var=xray_image control-1` returns it). So `{{ xray_image }}`
is safe to reference in the observability role. Trap avoided by verifying
rather than assuming.

---

### 3. Building the exporter image against a shell-less base

**Approach:** multi-stage build copying the pinned Xray client binary into
`python:3-slim`, so the exporter needs no `docker.sock` (rejected the
sock-mounted alternative — launching containers per scrape is wider privilege
than a visibility feature warrants).

**Snag:** the `ghcr.io/xtls/xray-core` image is distroless-style — no shell, so
`docker run --entrypoint sh` to introspect the binary path failed.

**Resolved:** extracted the binary via `docker create` + `docker cp` to confirm
the path (`/usr/local/bin/xray`) and that it is a **statically-linked** ELF —
which is why a plain `COPY --from` into `python:3-slim` runs without extra
libraries. Built and ran the image locally against `entry-1`'s live
StatsService *before* deploying, confirming correct metrics output and a
passing healthcheck.

---

### 4. THE real gap: Prometheus never reloaded on config change

**Symptom:** after `make platform` deployed cleanly and the exporter came up
healthy, the `xray-usage` job was absent from Prometheus's targets and the
metric didn't exist.

**Diagnosis:** the on-disk `prometheus.yml` *and* the copy inside the running
container both contained the new job — but Prometheus reads `scrape_configs`
only at startup. Its config is bind-mounted, so `docker compose up` does not
recreate it when only the file changes, and nothing else reloaded it. A latent
gap: no prior deploy had ever added a scrape job after first boot, so it had
never been exercised. (`file_sd`-based targets like `fleet-targets.yml` are
auto-reloaded by Prometheus and were never affected — only static
`scrape_configs`/rules need a reload.)

**Fix:** added a `Restart Prometheus` handler
(`roles/observability/handlers/main.yml`, previously an empty stub) that runs
`docker compose restart prometheus`, notified from the config render task.
Chose restart over `--web.enable-lifecycle` + POST `/-/reload` deliberately:
Prometheus's port 9090 is publicly exposed on `control-1` (a prior operator
decision), and the lifecycle API also exposes `/-/quit` — a public shutdown
endpoint we did not want to add. Reconciled the already-deployed state with one
manual `restart prometheus`; the handler prevents recurrence on future deploys.

---

### 5. DNS "bad address" — a red herring

**Symptom:** `docker exec observability-prometheus-1 wget xray-usage-exporter:9110`
returned `bad address`, and `nslookup xray-usage-exporter` returned "No answer",
suggesting the exporter was unreachable by service name.

**Nearly a wrong turn** — but a control test settled it: `nslookup blackbox`
*also* returned "No answer", even though Prometheus scrapes `blackbox:9115`
perfectly. So the failure was a **busybox `nslookup`/`wget` resolver quirk**
against Docker's embedded DNS, not a real resolution problem. Prometheus's
Go-based resolver resolves service names correctly (confirmed: both containers
share `observability_default`, and the exporter carries the
`xray-usage-exporter` network alias). The authoritative check — Prometheus's
own `/api/v1/targets` — showed the target `up` immediately after the restart in
#4. Lesson: verify against the component that actually matters (Prometheus's
resolver), not an unrelated debug tool in the same container.

---

### 6. End-to-end verification with real traffic

Generated the customer's client profile, ran a local Xray SOCKS client, and
pushed ~9 MB through the live tunnel (egress confirmed via the exit
`151.247.196.239`). Polled Prometheus until
`xray_user_traffic_bytes_total{email="customer-...",direction="down"}` rose from
1,227 to 9,035,494 bytes — matching the pushed volume — and confirmed the
dashboard's `topk(...)` query ranks the customer correctly. Cleaned up the test
client afterward.

---

## Summary

One genuine latent bug found and fixed (Prometheus not reloading on config
change — a repo-wide gap, not specific to this feature), one variable-scope
trap avoided by checking rather than assuming, and one misleading DNS symptom
correctly identified as a tooling artifact rather than chased. The feature is
visibility-only by design; enforcement and durable accounting remain the
backend's, per `BACKEND_INTEGRATION.md`.
