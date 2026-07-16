# V7 review — problems found and solutions applied

Chronological account of onboarding the first live customer and opening
observability access, and everything that surfaced while doing it. See
`CHANGELOG_V7.md` for the summarized version.

---

### 1. Customer onboarding

**Task:** Create a VPN user and get them a `vless://` link.

**Action:**
- Generated a UUID (`205e524d-1367-44ac-85a5-006d22e94f5e`) and an identifier
  (`customer-20260715-a7a1cd`).
- `make api-add UUID=... EMAIL=...` → added the runtime user to `entry-1`'s
  Xray API (`result: ok`).
- `./scripts/gen-client.sh --node entry-1 --uuid ... --email ... --api
  entry-1` → derived the REALITY client password from the deployed metadata
  manifest and emitted:
  `vless://205e524d-...@5.101.67.252:443?...&sni=vmshare.ru&pbk=...&sid=...#Spirit%20VPN%20entry-1`
- Verified with `xray-api.sh has` (exit code `0`).

No problems here — this part worked as designed on the first attempt.

---

### 2. Grafana unreachable

**Symptom:** Grafana dashboards ("VPN Fleet Overview", "VPN Logs") are
provisioned and exactly what was asked for (throughput, per-user stats,
server load), but Grafana (port 3000 on `control-1`) was firewalled to a
single WireGuard peer (`10.20.0.2`) only — not reachable from the operator
workstation, unlike Prometheus/Loki which V6 had already opened.

**Decision point:** Same tradeoff as V6's earlier port exposures. Asked
rather than assumed; operator chose to open it publicly, consistent with the
prior decisions and the documented no-hardening phase.

**Fix:** Added `3000` to both the `input` rule (`tcp dport { 9090, 3100,
3000 } accept`) and the `forward` rule (Grafana is Docker-bridge-published,
same as Prometheus/Loki) in `control-1`'s `/etc/nftables.conf`. Applied the
now-known-necessary two-step procedure from V6: `nft -f` reload, then
`systemctl restart docker` immediately after (since the reload's `flush
ruleset` always wipes Docker's NAT table). This time the restart did **not**
disturb any container — all eight showed unchanged multi-hour uptime
afterward, and traffic counters in the restored NAT table confirmed the fix
worked immediately.

**Verification:** `curl http://193.247.81.167:3000/api/health` →
`{"database":"ok","version":"12.0.2",...}`, HTTP 200.

*(This step was interrupted mid-verification by an operator laptop
power-off; re-verified after reconnecting — see #3.)*

---

### 3. Post-power-off sanity check

**Symptom:** The operator's laptop lost power mid-session, right as the
Grafana port-3000 change was being verified.

**Action:** On reconnecting, re-checked everything from scratch rather than
assuming the in-flight change had completed cleanly: external reachability
of 9090/3100/3000, Grafana health endpoint, all eight `control-1` container
statuses, and the Docker `ip nat` table contents. Everything came back clean
— the power-off happened after the fix had already landed, not during it.

---

### 4. Proactive audit of `entry-1`/`exit-fr` for the same pattern

**Prompted by:** An explicit request to check whether the same
Docker-networking pattern fixed on `control-1` in V6 also needed fixing on
the VPN nodes, rather than assuming host-networking made them immune.

**`entry-1` findings — worse than expected:**
- `nft list table ip nat` → `Error: No such file or directory`. The table
  didn't just have stale rules; it was **completely absent**.
- Root cause, traced back to V6: when `entry-1`'s port 10085 was opened
  publicly in V6, that was done with a raw `nft -f` reload — at that point
  in the V6 session, the "reload wipes Docker's NAT table" side effect had
  **not yet been discovered** (that discovery happened afterward, on
  `control-1`, in V6 issue #11). So `entry-1` never got the
  `systemctl restart docker` follow-up that would have restored it.
  This had zero visible symptoms since `entry-1`'s four services
  (`xray`, `nginx-mask`, `node-exporter`, `alloy`) all use
  `network_mode: host` and never depended on Docker-managed NAT.
- Additionally, `entry-1`'s `forward` chain had **no rules at all** beyond
  `ct state established,related accept` — not even the wg0-to-wg0
  management rule `control-1` has. Same latent-gap category as V6 #12/#13,
  just never exercised because nothing on this host needs bridge
  networking today.

**`exit-fr` findings — clean:**
- `nft list table ip nat` showed the expected structure and intact rules.
- Root cause of the difference: `exit-fr`'s port-10085 fix in V6 used
  `ufw allow 10085/tcp`, which manages rules incrementally and never issues
  a full ruleset flush. It was never at risk.

---

### 5. `entry-1` SSH went unresponsive mid-fix

**Symptom:** Attempting to apply the fix to `entry-1`, `ssh` hung and then
failed: `Connection timed out during banner exchange`. Repeated three times,
same result.

**Investigation:**
- Ruled out a general network outage: `control-1` and `exit-fr` both
  responded immediately on port 22 over the same period.
- Ruled out a firewall/network-path block on `entry-1` itself: a raw
  `/dev/tcp` connect to port 232 (its SSH port) succeeded immediately (SYN/ACK
  completes), but no SSH banner arrived within 8 seconds even when read
  directly from the socket — meaning the TCP layer was fine but `sshd` itself
  wasn't answering the application-level handshake.
- This pattern (TCP accepts, banner never arrives) is consistent with either
  a hung/overloaded `sshd`, or transient network jitter dropping the
  early banner packets specifically. Reported the finding rather than
  guessing further, and asked how to proceed.

**Resolution:** Operator's hypothesis (transient network lag) proved
correct — a retry loop with a short backoff succeeded on the very first
attempt (`alive-attempt-1`), and `uptime` showed
`load average: 0.35, 0.14, 0.09` with 19 days of continuous uptime,
confirming the host itself was never in distress.

---

### 6. Applying the `entry-1` fix

**Fix:** Same as `control-1`'s V6 fix — added
`ip saddr 172.16.0.0/12 accept` to `entry-1`'s `forward` chain. Applied with
`nft -c -f` (syntax check) → `nft -f` reload → `systemctl restart docker`
immediately after, this time proactively rather than reactively.

**Unexpected result:** Unlike the identical procedure on `control-1` (V6 and
#2 above), this restart **did** bounce all four containers — `docker ps`
showed fresh "Up 5 seconds" / "Up 6 seconds" uptimes instead of the
multi-hour uptimes they'd had before. Checked `/etc/docker/daemon.json` on
both hosts side by side: both have `"live-restore": true` set identically.
The cause of this asymmetry was not identified this session — flagged as an
open question in `CHANGELOG_V7.md`'s follow-up rather than guessed at.

**Verification:** Despite the restart, both external checks passed
immediately afterward: port 443 (VLESS) and port 10085 (Xray API) both open,
and all four containers reported `(healthy)` where a healthcheck applies.

---

### 7. `api-ping` failed — but not on `entry-1`

**Symptom:** Running `make api-ping NODE=entry-1` to confirm the API still
worked after the restart failed with: `failed to connect to the docker API
at unix:///var/run/docker.sock ... no such file or directory`.

**Root cause:** This error is from the **local** Docker daemon (the
operator's own workstation, which `xray-api.sh`/`gen-client.sh` shell out to
for parts of their work), not from `entry-1`. The earlier laptop power-off
(#3) had left local Docker inactive again — the same issue hit at the very
start of the V6 session, and `docker.service` is not enabled for auto-start
on this machine.

**Fix:** Operator ran `sudo systemctl start docker` (out of scope for the
assistant — entering sudo passwords is never done on the operator's behalf).
Re-ran `make api-ping` afterward: `Xray API reachable at 5.101.67.252:10085`.

---

### 8. Customer's runtime user was gone

**Symptom:** Anticipating the known P0-04 behavior from `CHANGELOG-V5.md`
("runtime users lost on restart" — Xray API-added users live in Xray's
in-memory HandlerService state, not in `config.json`, so they don't survive
a container restart), checked whether the customer onboarded in #1 survived
`entry-1`'s container bounce in #6.

**Confirmed lost:** `xray-api.sh has customer-20260715-a7a1cd` → exit code
`1`; `xray-api.sh emails` → empty list. The customer's already-issued
`vless://` link (containing that UUID) would fail to authenticate against
the entry node from this point on.

**Fix:** Re-ran `make api-add` with the **same** UUID and email used in #1.
Since the client's connection profile is keyed on the UUID (not on any
server-side session state), this restored access without the customer
needing a new link. Verified: `has` now returns exit code `0`.

**Why this wasn't caught proactively:** The forward-chain fix in #6 was
scoped as a firewall/networking change; its side effect of restarting
`xray` (and therefore clearing runtime users) wasn't obvious until checked
explicitly. `scripts/xray-reconcile.sh` (V5) exists precisely to make this
kind of recovery scriptable/idempotent for more than one user — recommended
as standard procedure going forward (see `CHANGELOG_V7.md` follow-up #3),
rather than manually tracking individual UUIDs to re-add after
infrastructure changes.

---

## Summary of what was a code bug vs. operational/infra event

| # | Issue | Category |
|---|-------|----------|
| 1 | Customer onboarding | Worked as designed |
| 2 | Grafana firewall scope | Pre-existing infra drift + operator decision |
| 3 | Power-off mid-verification | External event, no lasting effect |
| 4 | entry-1 NAT table missing since V6 | Incomplete fix from a prior session, closed out |
| 4 | entry-1 forward chain empty | Pre-existing infra drift, same class as V6 #12/#13 |
| 5 | entry-1 SSH banner timeout | Transient network lag, self-resolved |
| 6 | entry-1 container bounce on docker restart | Unexplained asymmetry vs. control-1, flagged open |
| 7 | Local Docker inactive again | Operator-environment dependency, recurring |
| 8 | Customer runtime user lost | Known product behavior (V5 P0-04), reconciled |

No application/Ansible code changes this session — everything was live
firewall configuration (closing out V6's own incomplete fix) and one
customer-continuity incident directly caused by that fix, resolved within
the same session.
