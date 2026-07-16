# Phase 0 findings — live-state divergences

What the capture ([README.md](README.md)) revealed vs. what the repo/docs assume.
These must be resolved before Phase 1 (codify + zero-diff), because you can't
reproduce state you don't understand.

## 1. SSH posture is inconsistent across the fleet ⚠ security

| Host | PermitRootLogin | PasswordAuth | Notes |
|---|---|---|---|
| control-1 | **`yes`** | **`yes`** | root **password** login enabled on the Vault/root-of-trust host |
| exit-fr | **`yes`** | **`yes`** | root **password** login enabled |
| entry-1 | `without-password` | `no` | already hardened (key-only) + `fail2ban` running |

**entry-1 is hardened; control-1 and exit-fr are not.** Root-password SSH on the
Vault host is the highest-priority gap. `trustedusercakeys none` everywhere — no
SSH CA trust yet (expected; that's future work).

## 2. control-1 runs an unmanaged native VPN-node stack ⚠ unexpected

`control-1` (supposed to be platform-only: Vault + observability) is also
running, **outside the repo**:

- native `xray.service` → `/usr/local/bin/xray` bound to **`*:443`**, config
  `/usr/local/etc/xray/config.json` (dated Jun 7), enabled, up 1d+.
- native `nginx.service` on `127.0.0.1:8443` (mask-style).

The platform containers (Prometheus/Loki/Grafana/Alertmanager/Vault) are the
`dockerd`-published sockets (3000/3100/9090/9093/8200/8201). The native
xray/nginx are a parallel, undocumented setup — most likely leftover from the
box previously serving as a node, or a manual side deployment. **Decision
needed:** is control-1 meant to double as a VPN node, or is this cruft to remove?
It affects whether the managed control-host firewall keeps `:443` open.

## 3. WireGuard overlay is partial

Hub = control-1 (`10.20.0.0/24`, :51820). Live peers:

| Overlay IP | Endpoint | Identity | State |
|---|---|---|---|
| 10.20.0.11 | 5.101.67.252 | **entry-1** | active (carrying telemetry, ~27 MiB) |
| 10.20.0.2 | 206.84.238.130 | admin/bastion (also in SSH allow rules) | active |
| 10.20.0.23 | 91.237.249.103 | unknown external peer | active |
| 10.20.0.21 | — | **intended exit-fr?** | **configured on hub, never handshakes** |

- **exit-fr is NOT on the overlay** — it has no `wg0` interface at all. It's
  reachable/managed only over its public IP + ufw. So "management over the
  overlay" currently does not cover exit-fr.
- **Two external admin peers** (`10.20.0.2`, `10.20.0.23`) exist on the hub but
  are **not modeled in the repo inventory**. Decision: bring them under
  `management_wireguard_external_peers`, or keep out-of-band.

## 4. Firewall / Docker config divergence

- Firewall engines differ: **nftables** on control-1/entry-1, **ufw** on exit-fr
  (already known; the managed layer must handle both or standardize).
  ✅ **RESOLVED (C3):** exit-fr migrated ufw → managed nftables (engine now
  uniform fleet-wide). `ufw` disabled at boot; managed `inet filter` is
  authoritative. Its data plane is host-networked (no DNAT), so it uses the same
  entry-1-model profile. Caveat: `ufw disable` leaves its `ip filter` chains
  loaded (shared with Docker → not force-removed live); they're non-authoritative
  and clear on reboot.
- **exit-fr has no `/etc/docker/daemon.json`** — Docker is unconfigured there,
  while control-1/entry-1 have `live-restore`, `no-new-privileges`,
  `userland-proxy:false`. This likely explains the container-restart behavior
  difference noted in `CHANGELOG_V7.md`.

## 5. Exposure ground truth (from listening sockets)

- control-1: `0.0.0.0` on 3000/3100/9090/9093 (the public stopgap openings) and
  `*:443` (native xray); Vault 8200/8201 correctly `127.0.0.1`-only.
- Confirms the overlay-first rollback targets: 9090/3100/3000 (+ 9093) should
  move off `0.0.0.0`.
- ✅ **RESOLVED (C2):** control-1's firewall is codified and public
  `9090/3100/3000` are removed → overlay-only. The sockets still *bind* `0.0.0.0`
  (dockerd-published), but the managed nftables ruleset now drops the public
  `tcp dport {3000,3100,9090}` accept in both input and forward chains; the ports
  are reachable only over `wg0` (Loki/Prometheus from the whole `10.20.0.0/24`,
  Grafana/Alertmanager/Vault from the workstation `10.20.0.2`). 9093 was already
  effectively closed publicly (no forward accept). Native `*:443` xray/nginx were
  retired earlier. Docker `ip nat` verified intact after apply.
- **exit-fr ran out-of-repo services on public ports** (parallels finding #2 on
  control-1): `server-stats.service` (gunicorn, public `151.247.196.239:8000`)
  and stale ufw allows for `8001/8091` (no listeners). ✅ **RESOLVED (C3):**
  managed firewall opens only `443` publicly (8000/8001/8091 dropped), and
  `server-stats.service` was **disabled + stopped** (operator-confirmed legacy).
  A second out-of-repo unit `lenza-telegram-export.service` runs but binds no
  public port — left untouched, noted for follow-up.

---

## Decisions (resolved) & progress

1. **control-1 native xray/nginx (:443, :8443):** confirmed **leftover** from
   when control-1 was a hand-managed exit node → **retire** (stop + disable).
2. **exit-fr onto the overlay:** ✅ **DONE** — installed WireGuard on exit-fr,
   generated an on-host keypair, brought up `wg0` at `10.20.0.21/32` (split
   tunnel, `AllowedIPs=10.20.0.0/24` so customer egress is unaffected), and
   swapped the hub's stale reserved `10.20.0.21` peer for exit-fr's real key
   (live + persisted, other peers untouched). Verified: handshake with hub,
   entry-1 stayed connected, and exit-fr reaches `10.20.0.1:9090/3100` over the
   overlay (the path Sub-step B needs). Done by hand (additive/safe); full
   WireGuard-role codification with zero-diff is deferred like the firewall.
3. **External WG peers** (`10.20.0.2`, `10.20.0.23`): **model in repo**
   (`management_wireguard_external_peers`) so the hub config is reproducible.
4. **SSH root+password on control-1 + exit-fr:** ✅ **DONE** — verified pure key
   auth first, then applied `/etc/ssh/sshd_config.d/00-spirit-hardening.conf`
   (`PasswordAuthentication no`, `PermitRootLogin prohibit-password`,
   `KbdInteractiveAuthentication no`), reloaded, and confirmed key login still
   works + password auth refused. Fleet SSH posture is now uniform (matches the
   already-hardened entry-1). **Still to do:** codify this drop-in into a
   `common` sshd task so a managed deploy reproduces it (zero-diff).

### Remaining convergence work (from these decisions)
- Retire control-1 native xray/nginx (decision 1).
- Onboard exit-fr to the overlay (decision 2) — a whole-fleet WG re-render.
- Model the two external peers (decision 3).
- Codify the SSH hardening + firewall + wg into managed tasks (Phase 1+).
