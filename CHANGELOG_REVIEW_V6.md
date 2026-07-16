# V6 review — problems found and solutions applied

This is a chronological account of `make deploy-e2e` run against the real
fleet (`control-1`, `entry-1`, `exit-fr`) for the first time, and everything
that had to be fixed — in code and on the live hosts — to get it to complete
cleanly. See `CHANGELOG_V6.md` for the summarized, categorized version.

---

### 1. Local render play tried to `sudo` on the workstation

**Symptom:** `make deploy-e2e` failed immediately in `make check`, before
touching any server: `MODULE FAILURE ... sudo: a password is required` on a
task (`Ensure secure render directory`) that only touches the local repo
checkout.

**Root cause:** `playbooks/render-check.yml`'s second play sets
`connection: local`, but the inventory sets `ansible_become: true` at the
`all:` level. `ansible_become` is a connection *variable*, and inventory
variables take precedence over a play's own `become:` keyword — a
non-obvious but documented Ansible precedence rule. The play's `become: false`
keyword was therefore silently overridden.

**Fix:** Added `ansible_become: false` under the play's `vars:` (a variable,
not a keyword, so it wins). Confirmed by re-running `render-check.sh`.

---

### 2. Docker daemon inactive

**Symptom:** The deep `xray -test` validation step in `render-check.sh`
failed: `failed to connect to the docker API at unix:///var/run/docker.sock`.

**Root cause:** Docker wasn't running on the operator workstation.

**Fix:** `sudo systemctl start docker` (operator-run; password entry is out
of scope for the assistant to perform). Not a code issue.

---

### 3. `entry-1` had a leftover native `xray.service` and native `nginx`

**Symptom:** Once SSH access and the Docker daemon were sorted, the actual
`make deploy` run reached `entry-1` and `exit-fr` and both crash-looped:
`vpn-xray-1` logged `permission denied` reading `/etc/xray/config.json`;
`vpn-nginx-mask-1` logged `bind() to 127.0.0.1:8443 failed (Address in use)`.

**Root cause (nginx):** A bare-metal `nginx` (`systemctl status nginx`
showed `active`, started well before this session) was already bound to
`127.0.0.1:8443` on `exit-fr`, unrelated to and predating the Dockerized
`nginx-mask` service this repo manages.

**Root cause (xray permission):** Separate issue, see #4.

**Fix:** `systemctl stop nginx && systemctl disable nginx` on `exit-fr`
(confirmed operator decision before acting, since this is a live production
service). Later found the same pattern for `xray.service` on `entry-1`
(native `xray` bound to `:443`, blocking `vpn-xray-1`) — same fix,
`systemctl stop xray && systemctl disable xray`, again confirmed first.

---

### 4. `vpn-xray-1` crash loop: config permission denied

**Symptom:** `docker logs vpn-xray-1` → `open /etc/xray/config.json:
permission denied`, container stuck `Restarting`.

**Root cause:** `config.json` is rendered `root:root`, mode `0640`
(`roles/xray/tasks/main.yml`), but
`roles/vpn_stack/templates/compose.yml.j2` never set a `user:` for the
`xray` service, so it ran as the `ghcr.io/xtls/xray-core` image's default
non-root UID (`65532`), which cannot read a `root:root` file it isn't in the
group for. The render task's own `validate:` command
(`docker run --rm --user 0:0 ...`) explicitly forces root — which is exactly
why this was never caught: the *validation* ran as root, the *runtime
service* didn't.

**Fix:** Added `user: "0:0"` to the `xray` service in
`roles/vpn_stack/templates/compose.yml.j2`, matching what was already being
validated.

---

### 5. `vpn-xray-1` crash loop again: port 443 already in use

**Symptom:** After fixing #4, `entry-1`'s `vpn-xray-1` still crash-looped:
`listen tcp 0.0.0.0:443: bind: address already in use`.

**Root cause:** The leftover native `xray.service` from #3, still holding
`:443` at the time.

**Fix:** Covered by the `systemctl stop/disable xray` in #3.

---

### 6. `inventory_dir` undefined in `wire-fleet.yml`

**Symptom:** `Create per-entry host_vars directories` failed:
`'inventory_dir' is undefined`.

**Root cause:** `inventory_dir` is a magic variable populated per-host from
the inventory source that defined that host. The task runs on `localhost`,
which is never declared in `inventories/prod/inventory.yml` — it's Ansible's
*implicit* localhost — so it never gets that variable populated. Verified
directly: `ansible -i inventories/prod/inventory.yml localhost -m debug -a
"var=inventory_dir"` → `VARIABLE IS NOT DEFINED!`, versus a real inventory
host returning the correct path.

**Fix:** Replaced `inventory_dir` with a play-local
`wire_inventory_dir: "{{ ansible_inventory_sources | first | dirname }}"`,
which *is* populated for every host including implicit localhost (verified
the same way). Applied everywhere `inventory_dir` was referenced in
`playbooks/wire-fleet.yml`.

---

### 7. First-time Docker image pull hidden by `no_log` looked like a config bug

**Symptom:** `xray : Render and validate Xray configuration with the pinned
runtime image` failed on `entry-1` with `no_log: true` censoring the actual
error.

**Investigation:** Reproduced the exact `validate:` command
(`docker run --rm --user 0:0 -v ... run -test -config ...`) directly on
`entry-1` and it succeeded immediately — but had to pull the ~20-layer
`ghcr.io/xtls/xray-core:26.3.27` image first, since it had never been used on
that host before. `no_log: true` is intentional here (the config file embeds
REALITY keys), so the real Ansible error was never visible; the most likely
explanation is the cold pull, combined with whatever timeout/network
conditions applied inside the `validate:` subprocess, made the first attempt
fail.

**Fix:** No code change — warmed the image cache by pulling it manually once.
Re-running the deploy after that passed the step cleanly and repeatably.

---

### 8. Entry REALITY client-password regex silently extracted nothing

**Symptom:** `playbooks/client-metadata.yml`'s `Require a valid non-private
client password` assertion failed: `entry_reality_password | length == 43`
evaluated false — repeatably, across multiple re-runs, unlike #7.

**Investigation:**
- Reproduced the exact derive command (`docker run ... x25519 -i <real
  private key>`) via Ansible ad-hoc against `entry-1` — succeeded
  deterministically every time, ruling out a transient/environmental cause.
- Wrote a scoped diagnostic playbook replaying the real task sequence with a
  `debug` task dumping the raw command output and the extracted password:
  stdout was exactly as expected (`Password (PublicKey): ...`), but the
  extracted `entry_reality_password` was empty.
- Isolated it to the regex itself: the pattern was written with doubled
  backslashes (`\\s`) inside a YAML **folded** scalar (`>-`). YAML
  plain/folded scalars do not process backslash escapes at all (unlike
  double-quoted scalars), so `\\s` survives into the template text as two
  literal backslash characters. Confirmed empirically with a minimal
  isolated test: the same pattern with `\\s` returns `[]`; the identical
  pattern with `\s` correctly extracts the password.
- This is the **exact same bug class** already fixed once in
  `roles/xray/tasks/main.yml` (documented in
  `CHANGELOG-E2E-HOTFIX-4.md`, and reflected in the safe, comment-annotated
  patterns in `roles/xray/defaults/main.yml`: *"These patterns intentionally
  avoid doubled Jinja backslash escapes, which previously made valid command
  output unparseable."*) and correctly avoided in the exit-side derivation in
  `playbooks/wire-fleet.yml`. It was reintroduced in `client-metadata.yml` — a
  newer file, not covered by `playbooks/reality-key-parser-test.yml`'s
  regression coverage (which only exercises the exit-side parser).
- Notably, a **correct** copy of the same pattern
  (`_reality_public_key_output_regex`) was already sitting in the same play's
  `vars:` block, unused — presumably copy-pasted from `wire-fleet.yml` as a
  reference and then not actually wired up.

**Fix:** Replaced the broken inline regex with a reference to the existing,
correct `_reality_public_key_output_regex` var. Verified by re-running
`client-metadata.yml` standalone against `entry-1`: the assertion now passes
and `generated/client-endpoints.json` is written.

---

### 9. Public Xray API (10085) unreachable from the deployment controller

**Symptom:** `verify.yml`'s `Wait until every public customer endpoint /
Xray API is reachable` step timed out after 30s for both `entry-1` and
`exit-fr`.

**Investigation:** Confirmed Xray itself was listening (`ss -tlnp` showed
`*:10085`). Found an active, `policy drop` nftables ruleset on `entry-1`
(not applied by this repo — `common_manage_firewall: false`) restricting
10085 to the WireGuard management network (`10.20.0.0/24` via `wg0`) only.
`exit-fr` had the equivalent restriction via `ufw` (no rule for 10085 at
all, falling through to the default-deny policy).

**Decision point:** This contradicts the repo's documented intent
(`xray_api_public_mode: true`, and the inventory comment *"the Xray gRPC API
is intentionally public on TCP/10085 for the current no-hardening phase"*),
but a pre-existing firewall had already locked it down — a legitimate
security tradeoff, so the operator was asked rather than assumed. Operator
chose to open it publicly, matching the documented phase.

**Fix:** `entry-1`: appended a `tcp dport { 10085 } accept` rule to
`/etc/nftables.conf` and reloaded. `exit-fr`: `ufw allow 10085/tcp`. Verified
externally reachable from the operator workstation afterward.

---

### 10. Same problem, Prometheus/Loki ingestion this time

**Symptom:** After #9, the run progressed further and failed later:
`Wait until Prometheus contains every node's node_exporter metrics` — query
returned an empty result set.

**Root cause:** Same pattern as #9. `control-1` also has a pre-existing
`policy drop` nftables ruleset restricting Prometheus (9090) and Loki (3100)
to the WireGuard network. But `entry-1`/`exit-fr` push metrics/logs
(`prometheus_remote_write`, `loki_ops_endpoint`) to `control-1`'s **public**
IP, not its WireGuard IP — so the pushes never arrived.

**Decision point:** Same tradeoff as #9; operator again chose to open the
ports publicly for consistency with the earlier decision.

**Fix (intended):** Appended `tcp dport { 9090, 3100 } accept` to
`control-1`'s `/etc/nftables.conf` `input` chain and reloaded. This is where
it got more complicated — see #11 and #12.

---

### 11. Self-inflicted: `nft -f` wiped Docker's own NAT table

**Symptom:** After the "fix" in #10, ports 9090/3100 were *still*
unreachable from the outside, and `curl` against Prometheus locally on
`control-1` also stopped working.

**Root cause:** `/etc/nftables.conf` starts with `flush ruleset`. Reloading
it via `nft -f` does not just replace this repo's own `inet filter` table —
it flushes the **entire** nftables ruleset, including the separate `ip nat`
table that Docker's `iptables-nft` backend maintains for every
port-published container (Prometheus, Loki, Grafana, Vault, Alertmanager all
use bridge networking with published ports, unlike the VPN stack on
`entry-1`/`exit-fr`, which uses `network_mode: host` and has no such
dependency — those hosts were unaffected by the equivalent reload in #9).
Docker does not automatically notice and reconcile externally-flushed rules.

**Impact:** Every Docker-published service on `control-1` lost its
port-forwarding, including over WireGuard — a brief regression introduced by
this session's own troubleshooting, caught immediately by the next
verification step rather than left unnoticed.

**Fix:** `systemctl restart docker`, which reprograms Docker's network rules
from its own state without restarting any container (confirmed: all
containers retained their original uptime, and traffic counters in the
restored NAT table showed flow resuming immediately). Verified `nft list
table ip nat` showed the expected DNAT rules again, and external port checks
plus `curl http://<control-1>:9090/-/ready` succeeded.

---

### 12. Deeper self-inflicted: the `forward` chain never allowed real container traffic

**Symptom:** Ports 9090/3100 were externally reachable again after #11, but
Prometheus's own scrape of its local `node-exporter` container was **down**
(`Get "http://node-exporter:9100/metrics": context deadline exceeded`), and
`docker exec` from Prometheus to node-exporter — by container name *and* by
raw IP — both timed out.

**Root cause:** The `docker restart` in #11 reset conntrack state for
existing container-to-container connections. Once reset, it became apparent
that this firewall's `forward` chain (`policy drop`) had **never** contained
a rule permitting plain intra-bridge container traffic — only
already-established connections (from before any of this session's changes)
were passing, via `ct state established,related accept`. This is a
pre-existing gap in the manually-maintained live firewall, not something
introduced this session — it was simply never exercised until the restart
in #11 reset that state.

**Fix (first pass):** Added `ip saddr 172.16.0.0/12 ip daddr 172.16.0.0/12
accept` to the `forward` chain (Docker's private bridge address range).
Verified: `platform-node` scrape target went `up`, and `node_uname_info`
started returning results for `control-1`.

---

### 13. Same gap, wider: `blackbox-exporter` couldn't reach the internet either

**Symptom:** `Wait until every public customer endpoint is reachable from
Prometheus` — `sum(probe_success{service="vless"})` stayed at `0` even
though the scrape targets themselves were `up`.

**Root cause:** The fix in #12 only allowed traffic where **both** source
and destination are inside the Docker bridge range. `blackbox-exporter`'s
probes are *outbound* — from a container IP (bridge range) to the public
internet IPs of `entry-1`/`exit-fr` (not bridge range) — so they still hit
the same default-drop policy. Confirmed directly:
`docker exec observability-blackbox-1 wget ... http://5.101.67.252:443/`
timed out.

**Fix:** Widened the rule to `ip saddr 172.16.0.0/12 accept` (source only,
any destination) — the standard "containers can reach the internet" pattern.
Verified: the same `wget` now got an HTTP response (`400 Bad Request` — the
target is a raw TLS/VLESS port, not HTTP, so a protocol-level response was
itself confirmation that the TCP path now works), and
`probe_success{service="vless"}` / `{service="xray-api"}` all flipped to `1`
for both `entry-1` and `exit-fr`.

**Note:** `roles/common/templates/nftables.conf.j2` — the Ansible-managed
firewall template this repo would apply if `common_manage_firewall` were
turned on — has the identical gap: no rule in its `forward` chain permits
general Docker-bridge egress. It was **not** modified this session (it isn't
currently in effect on any host), but will reproduce this exact class of
outage the day firewall management is enabled. Flagged in
`CHANGELOG_V6.md`'s follow-up section; not fixed here since it's a
prospective/template issue rather than something blocking this deploy.

---

### 14. Final validation

With all of the above resolved:

- `make deploy-e2e` completed with a clean `PLAY RECAP` across all four hosts
  (`control-1`, `entry-1`, `exit-fr`, `localhost`) — zero `failed`.
- `make e2e-all ENTRY=entry-1` ran the full customer-flow smoke test:
  provisioned a throwaway user via the Xray API, generated its client config,
  connected through `entry-1` over a local SOCKS proxy, confirmed observed
  egress IP matched `exit-fr`'s public IP, pulled per-user traffic counters,
  removed the user via the API, and confirmed a fresh connection attempt was
  rejected. Result: `E2E ALL EXITS PASS: exit-fr`.

---

## Summary of what was a code bug vs. live-infrastructure drift

| # | Issue | Category |
|---|-------|----------|
| 1 | render-check.yml become leak | Code bug |
| 2 | Docker daemon inactive | Local environment |
| 3 | Leftover native nginx/xray services | Pre-existing infra drift |
| 4 | xray container permission denied | Code bug |
| 5 | (same as 3/4) | — |
| 6 | inventory_dir undefined | Code bug |
| 7 | Cold image pull under no_log | Environmental (no fix needed) |
| 8 | client-metadata.yml regex | Code bug (recurrence) |
| 9 | Xray API firewall scope | Pre-existing infra drift + operator decision |
| 10 | Prometheus/Loki firewall scope | Pre-existing infra drift + operator decision |
| 11 | nft flush wiped Docker NAT | Session-caused regression, self-corrected |
| 12 | forward chain missing bridge rule | Pre-existing infra drift, exposed by #11 |
| 13 | forward chain missing egress rule | Pre-existing infra drift, exposed by #11 |

Four genuine Ansible/template code bugs (#1, #4, #6, #8), three fixed inside
this repo's roles/playbooks and one already-correct-but-unused variable
finally wired up. The rest was live-host firewall configuration that
predates and diverges from what this repo currently manages — including two
issues (#11, #12/#13) that this session's own troubleshooting exposed by
resetting connection state that had been masking them.
