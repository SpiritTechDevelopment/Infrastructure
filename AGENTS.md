# AGENTS.md — repository assessment

Assessment of `infra_v1` at `main` (`84e066a`), 22 August 2026.

Scope: current state, validity of the CI/CD model, network/topology and port
exposure, host and container hardening, and internal consistency of sources and
flows.

This file deliberately contains **no addresses, hostnames, port numbers, keys or
digests**, per the repository's own rule ("Не коммитить открытые IP, домены,
порты, сертификаты, токены или ключи"). Concrete values are referenced by their
desired-state key path instead. Every claim below is anchored to a file and,
where useful, a line.

---

## 1. What this repository is

A GitOps control repository for a VPLESS/REALITY VPN fleet. Direction of change
is Git → management host → servers; observed server state is never written back
as desired state. Three layers:

| Layer | Owner | Location |
|---|---|---|
| Desired state (what should exist) | SOPS-encrypted YAML | `desired/` |
| Compiler (desired state → artifacts) | Python | `fleetctl/` |
| Mechanism (artifacts → hosts) | Ansible | `roles/`, `playbooks/` |

`fleetctl` compiles the encrypted topology into per-node plans, an Ansible
inventory, a control plan, monitoring targets, a DNS plan and a backend
manifest. Ansible consumes only those compiled artifacts —
`roles/compiled_node_plan` is the single mapping point from plan to role inputs
and refuses to run against a plan that does not match its inventory host
([tasks/main.yml lines 15-40](roles/compiled_node_plan/tasks/main.yml)).

### Verified state

- **Tests**: 304 pass, 7 skipped (`SPIRITVPN_SKIP_LIVE_DESIRED=1`) — 296 at the
  time of the original assessment, plus eight added with the CI-1 fix.
- **Lint**: `make lint` clean — ansible-lint reports 0 failures across 115 files
  and notes the stricter `production` profile also passes.
- **Working tree**: `README.md` and `docs/operations/INFRA_V1_GUIDE_RU.md`
  modified and uncommitted (a large rewrite: ~1200 insertions, ~2160 deletions).
- **Live shape** (from the local `build/develop/` artifacts, which are
  gitignored derivatives): one control/management host, one entry node, one exit
  node, joined by a WireGuard management overlay; images pinned by digest for
  the node-side components.

Overall this is a well-above-average infrastructure repository. The security
model is coherent and mostly enforced in code rather than in prose. The findings
below are real, but they sit on a solid base.

---

## 2. CI/CD model

### 2.1 The model is sound

The core design is correct and unusually careful:

- **Split trust.** Public GitHub-hosted runners never hold a decryption
  identity. They validate SOPS envelope *structure* and compile a plaintext
  fixture (`ci.yml`, job `desired-state`). Only the dedicated self-hosted runner
  binds the age identity and compiles the real topology (`ci.yml`, job
  `trusted-desired-state`, gated on `github.event_name == 'push'`).
- **Exact-SHA handoff.** No deployment picks "latest". Each deploy workflow
  checks out an explicit 40-hex SHA, asserts `git rev-parse HEAD` equals it, and
  asserts `merge-base --is-ancestor` against `origin/main`
  ([fleet-deploy.yml lines 594-601](.github/workflows/fleet-deploy.yml)).
- **No credentials cross the boundary.** The runner sends a signed Git *bundle*
  over stdin to a forced-command SSH endpoint. `scripts/platform-remote.sh`
  validates every argument by anchored regex and execs `ssh` with
  `IdentitiesOnly`, `StrictHostKeyChecking=yes`, `BatchMode`, `-F /dev/null`.
  The hub's `authorized_keys` binds each key to one environment via
  `restrict,command=`
  ([platform_executor/templates/authorized_keys.j2](roles/platform_executor/templates/authorized_keys.j2)),
  and `spiritvpn-github-command` re-validates every field server-side. Both
  sides validate independently — correct, not redundant.
- **Deployment ref promotion is properly isolated.** `contents: write` lives in
  exactly one job (`promote`) that has no SSH key, no bundle and no hub access,
  and it uses compare-and-swap on both ends (`git update-ref` with expected old
  value, then `push --force-with-lease` naming the prior SHA)
  ([fleet-deploy.yml lines 747-778](.github/workflows/fleet-deploy.yml)).
- **Zero exit code is not treated as success.** `deployment-record.py` parses
  the executor transcript to decide whether the backend actually accepted the
  manifest before the ref may advance
  ([fleet-deploy.yml lines 682-702](.github/workflows/fleet-deploy.yml)).
- **Actions pinned by commit SHA**, with the version in a trailing comment.
- **`prod` is excluded from the automatic path** with the reason recorded at the
  decision point, not hidden in a trigger filter
  ([desired-state-deploy.yml lines 406-424](.github/workflows/desired-state-deploy.yml)).

### 2.2 Finding — CI-1: multi-commit pushes are silently under-deployed (high)

`desired-state-deploy.yml` decides which contours to reconcile by diffing
`BEFORE..AFTER`. `AFTER` is the CI head SHA. **`BEFORE` is never set** — no
`env:` entry, no earlier step defines it — so line 64-65 always falls through to:

```
BEFORE="$(git rev-parse "$AFTER^" ...)"
```

`BEFORE` is therefore always the *first parent of the head commit*, not the
branch state before the push.

Consequences:

1. A push containing N > 1 commits is analysed as if only the last commit
   existed. Changes in the earlier N-1 commits do not appear in
   `git diff --name-only` and their contour is never deployed. The run is green.
   This is precisely the "тихий недовыкат" the surrounding comments say the
   design exists to prevent, and it contradicts the file's own header claim to
   be a level-triggered reconcile — the detection step is edge-triggered.
2. The zero-SHA branch at line 82 (`[[ "$BEFORE" =~ ^0+$ ]]`) is unreachable:
   `$AFTER^` resolves for every non-root commit.

The repository's normal workflow (`README.md` §"Обычный путь изменения": "Сделать
коммит и отправить его") does not require one commit per push, so this is
reachable in routine use. It has likely been masked so far because the automated
release path (`release-bump.yml`) pushes exactly one commit at a time.

**Fixed.** A `baseline` step now resolves the head SHA of the last *successful*
run of this workflow on `main` via the Actions API and passes it to `split` as
`BEFORE`; `split` refuses to run without one instead of inventing a default.
One push produces one CI run and one reconcile run, so that SHA is exactly the
state before the current push, and a failed run leaves its delta in the next
comparison — the level-triggered behaviour the header promised. With no
successful run in history the baseline becomes the empty tree, which marks every
path changed and reconciles all contours rather than guessing.

Why the defect survived a dedicated test suite is worth recording:
`tests/unit/test_desired_state_detect.py` executes the real `split` script
against throwaway repositories, but **injects `BEFORE` itself**. Production took
the `test -z` fallback; the tests never did. Both sides were correct about the
code they could see. Two behavioural tests now cover a multi-commit push (one
asserting full coverage, one pinning the old lossy behaviour as a defect
snapshot), plus two guards that `split` is handed a baseline and fails closed
without it, plus four on the `baseline` step itself.

Those four exist because the first version of this fix had its own defect: the
API call sat inside a process substitution, whose exit status is invisible to
both `set -e` and `pipefail`. A transient API error would have read as "no
successful run" and reconciled every contour on a network blip. The query is now
a separate command whose failure stops the step, and the tests pin all four
cases apart: a reachable run, a commit lost to force-push, an empty history, and
a failing call.

### 2.3 Finding — CI-2: dead branch in contour detection (low)

The `*/environment.yml` case
([desired-state-deploy.yml lines 376-387](.github/workflows/desired-state-deploy.yml))
splits control from fleet by comparing `.spec.control` subtrees with `yq`. No
`environment.yml` exists under `desired/environments/*/` — only
`topology.sops.yml`. The branch is unreachable, and it silently depends on `yq`
being present on the runner image.

### 2.4 Observation — CI-3: the release trust chain reaches outside this repo

`release-bump.yml` runs on `repository_dispatch` **on the self-hosted runner**,
decrypts the topology, and pushes straight to `main` with `INFRA_PUSH_TOKEN`,
bypassing pull request and code-owner review. For `develop` the resulting commit
then auto-deploys.

This is a deliberate, documented decision and the blast radius is well
constrained: the payload is validated field-by-field with anchored regexes
(line 1014-1041), and `Refuse an unexpected change` (line 1104-1107) fails the
job if the diff touches anything but the one topology file. The residual fact
worth stating plainly: **any repository or token that can send a
`repository_dispatch` to this repo can move a running image digest in `develop`
without human review.** That is an accepted risk, not a defect — but it should
be an explicit one, and the dispatch token's scope and rotation belong in the
operator guide.

### 2.5 Observation — CI-4: environment separation rests on SSH, not GitHub

`environment:` is absent from every deploy job because GitHub Environments are
unavailable on the org's plan. The workflows document this thoroughly. The
actual separation — forced command bound to one environment, plus server-side
revalidation — is real and adequate. But there is consequently **no approval
gate anywhere in the system**: `prod` is protected only by being filtered out of
the automatic path and requiring a manual `workflow_dispatch`. Anyone who can
dispatch a workflow can apply `prod`. Worth re-testing whenever the plan changes.

### 2.6 Minor

- `platform-readiness.yml` checks out the default branch with no `ref:` pin,
  unlike every other workflow. Low impact (it only runs a read-only remote
  command), but inconsistent.
- `desired-state-deploy.yml` correctly gates on
  `head_repository.full_name == github.repository`, blocking fork-sourced runs.

---

## 3. Topology, ports and network exposure

### 3.1 The bind discipline is the strongest part of the repository

Exposure is decided in one place (`roles/compiled_node_plan/tasks/main.yml:110-215`)
and then enforced at three independent layers:

1. **Bind address.** Every observability listener is bound to the node's
   management-overlay address; Vault, the Xray gRPC API, the Xray metrics
   endpoint and the Alloy UI are loopback-only. Wildcards are never used.
2. **Firewall.** `common_restricted_tcp_rules` additionally scopes the agent and
   metrics ports to the management network *and* the overlay interface — with a
   comment stating explicitly that this exists so the firewall is not the single
   thing between `/metrics` and the internet if a bind is ever loosened.
3. **Readiness.** The playbook re-asserts the bind and *fails* on a wildcard
   rather than warning
   ([operations/readiness.yml lines 135-152](playbooks/operations/readiness.yml)).

Three layers that each independently prevent the same disclosure, with the
reasoning recorded. This is the right pattern and it is applied consistently.

Public ingress on a traffic node is limited to the declared data port
(`logical_node.public.port`), SSH scoped to the management network in steady
state, and the WireGuard port (see NET-1). REALITY's `dest` points at a
loopback-only nginx masquerade — the standard self-hosted mask, correctly wired.
Xray routing blackholes `geoip:private` before anything else, which prevents a
customer from using an exit node to reach the operator's own RFC1918 space.

### 3.2 Finding — NET-1: the WireGuard port is open to the internet unnecessarily (medium)

`common_public_udp_ports` is set to the management listen port on every traffic
node ([compiled_node_plan/tasks/main.yml line 129](roles/compiled_node_plan/tasks/main.yml)),
which the nftables template accepts from **any source** with no CIDR scoping
([nftables.conf.j2 lines 38-40](roles/common/templates/nftables.conf.j2)).

But nodes do not need an inbound listener. The node's peer stanza carries
`Endpoint = <hub>` and `PersistentKeepalive`
([bootstrap_wireguard/templates/configure-wireguard.sh.j2 lines 16-21](roles/bootstrap_wireguard/templates/configure-wireguard.sh.j2)),
while the hub's peer stanzas for nodes carry only `PublicKey` and `AllowedIPs`
with no endpoint
([platform_wireguard/templates/base.conf.j2](roles/platform_wireguard/templates/base.conf.j2)).
The node dials the hub and the hub learns the node's endpoint from the
handshake. Traffic is entirely node-initiated.

So every node in the fleet answers WireGuard on a well-known UDP port from the
whole internet for no operational reason. WireGuard is silent to unauthenticated
packets, so this is not directly exploitable today. It is still:

- unnecessary attack surface against any future WireGuard vulnerability, and
- a fleet-wide correlation signal: identical UDP port behaviour across nodes
  whose entire public purpose is to look like ordinary TLS web servers. This
  partially undermines the REALITY masquerade the rest of the design invests in.

**Fixed (firewall half).** `common_public_udp_ports` is now empty for traffic
nodes. Existing tunnels are unaffected: the hub's replies match the conntrack
entry the node's own outbound packet created, and `PersistentKeepalive` is
shorter than the UDP conntrack timeout, so the flow never idles out.

**Still open:** `ListenPort` remains pinned to the same well-known value on
every node, so it is also the *source* port of the outbound tunnel — visible to
any on-path observer and uniform across the fleet. Unpinning it means a
WireGuard interface restart on live nodes, so it belongs in a deliberate change
rather than alongside a firewall edit.

### 3.3 Finding — NET-2: ICMP is treated as one undifferentiated class (medium)

```
ip protocol icmp limit rate 20/second accept
ip6 nexthdr ipv6-icmp limit rate 20/second accept
```
([nftables.conf.j2 lines 18-19](roles/common/templates/nftables.conf.j2))

**Correction to an earlier draft of this document:** these rules sit *after*
`ct state established,related accept` (line 13). Conntrack marks ICMP errors
that reference a tracked flow as `related`, so `fragmentation-needed` and
`packet-too-big` for existing connections are accepted before they ever reach
the limiter. Path MTU Discovery for tracked flows is **not** broken. The
original claim here was too strong.

What the shared 20/s bucket actually governs is ICMP that conntrack sees as
`NEW` or untracked. That leaves two real problems:

1. **IPv6 Neighbour Discovery is rate-limited.** NS/NA/RS/RA are `NEW`, so they
   do hit the limiter, and they are mandatory for IPv6 to function on the link
   at all. A single host sending 20+ pps of any ICMPv6 starves ND for the whole
   interface. IPv6 is not currently declared in desired state, so this is latent
   rather than live — but it is a trap for whoever enables it, and the failure
   mode (intermittent, load-dependent link loss) is very hard to attribute.
2. **Echo-request is accepted from anywhere.** Every node answers ping from the
   entire internet. This is the anonymity problem, not a availability one — see
   the fleet-enumeration discussion in §3.8.

ICMP errors for *untracked* flows are also rate-limited, which is a minor
residual correctness issue but not the blackhole the first draft described.

Related: exactly one live ICMP dependency exists — `bootstrap_wireguard`
(`tasks/main.yml:151`) gates overlay convergence on a ping from the node to the
hub's overlay address. That ping is accepted on the hub by
`iifname "<overlay>" accept` (the hub sets `common_trusted_interfaces`;
traffic nodes do not), **not** by the generic ICMP rule. Dropping public echo
therefore does not break bootstrap, provided node egress echo to the management
network is preserved. Verified, not assumed.

**Fixed.** The single bucket is replaced by four classes: ICMP/ICMPv6 errors
accepted unconditionally, IPv6 ND and MLD accepted unconditionally (no rate
limit), echo accepted only from `common_icmp_echo_cidrs` under a 5/s limit, and
everything else dropped — silently, since a rejection confirms the host as well
as a reply does. Timestamp goes with echo, closing a clock-correlation channel.
The `output` chain gained the second direction: echo-request leaves only toward
trusted interfaces and declared echo networks, so a node neither answers nor
emits public ping. Error signalling still leaves the host, deliberately —
suppressing it would break PMTUD for clients connecting *to* the data port.

Verified in a network namespace against the rendered ruleset, not by
inspection: overlay ping succeeds in both directions (so bootstrap survives),
public ping is unanswered inbound and dropped outbound, the public data port
stays reachable, and the metrics port stays blocked from a public source.

### 3.4 Finding — NET-3: the two-path reachability model exists only in dead code (medium)

The user's "dual ping" question has a precise answer, and it is not the
reassuring one.

`fleetctl/readiness/suite.py:19-24` defines two reachability gates —
`host_reachable` against the node's **public** address and
`management_address_reachable` against its **overlay** address. That is a
correct two-path model: it distinguishes "the box is up" from "the box is up and
the overlay converged".

**Nothing calls it.** `build_gate_specs` and `GateRunner` are imported only by
`tests/unit/test_readiness.py`. `fleetctl/cli.py` never references the module;
no playbook does either. `ReadinessProbe` is a Protocol with no implementation
anywhere in the repository.

The readiness that actually runs is `playbooks/operations/readiness.yml`, and it
checks the overlay path only (`ip -4 address show dev <iface>`, line 33-47). The
public path is never verified from off-host. Combined with §3.6, **no automated
check anywhere confirms that a node is reachable on its public address from the
internet.** A node whose public interface, DNS record or data-port listener is
broken can pass readiness and be promoted.

This is the most consequential consistency defect in the repository: a
well-designed safety mechanism exists, is unit-tested, and is not wired in. The
tests pass, which makes it look done.

### 3.5 Finding — NET-4: the public-port readiness assertion is a substring match (low)

```yaml
- "(':' ~ (…public.port | string)) in _readiness_listeners.stdout"
```
([operations/readiness.yml lines 128-132](playbooks/operations/readiness.yml))

This searches the whole `ss -H -lnt` output for `:<port>` as a substring. It
passes if *anything* listens on a port whose decimal representation starts with
those digits, on *any* address — including loopback-only. A data port bound to
`127.0.0.1` would satisfy it.

The contrast with the metrics assertion twenty lines below — which matches
`address:port` exactly and explicitly rejects `0.0.0.0:` and `*:` — shows the
author knew the strict form. Apply it here too.

### 3.6 Finding — NET-5: compiled probe targets are never consumed (medium)

`fleetctl` compiles targets in three collections, all with
`readiness_expected: true`:

| Collection | Kind | Consumed by |
|---|---|---|
| `management` | `metrics` | Prometheus, via `control_observability` file_sd |
| `management` | `health` (agent gRPC) | **nobody** |
| `external` | `probe` (public data port) | **nobody** |
| `node-local` | `metrics` (Xray) | **nobody** |

`roles/control_observability/tasks/main.yml:72-78` selects
`kind == 'metrics' and collection == 'management'` and discards the rest.
`platform_observability_jobs` declares four jobs, none of them a prober. And
`blackbox_exporter` is pinned by digest in `desired/common/components.yml` and
shipped in every compiled node plan, but **grep finds no reference to it in any
role, playbook, template or compose file** — it exists only in generated
artifacts.

So the external reachability probe of the public data port — the one thing that
would independently detect the gap in §3.4 — is compiled, declared
readiness-expected, and silently dropped. Likewise the agent health target and
Xray's own metrics.

Either wire up blackbox_exporter and a probe job, or stop compiling targets
nothing reads. Declaring a probe that does not run is worse than not declaring
it: it reads as coverage.

### 3.7 Finding — NET-6: exit-node readiness discloses node addresses to a third party (low)

`spiritvpn_smoke_echo_url: https://api.ipify.org`
([compiled_node_plan/defaults/main.yml line 22](roles/compiled_node_plan/defaults/main.yml)),
used at `tasks/main.yml:231` to confirm an exit node egresses under its own
address.

The trade-off is acknowledged in a comment, and the check itself is meaningful
(it proves egress is not proxied or NATed). But for a VPN operator this means
every readiness run reveals the fleet's exit addresses, correlated in time, to a
third-party service with its own logs. For `prod` this deserves a self-hosted
echo endpoint on infrastructure already being maintained.

### 3.8 Finding — ANON-1: the REALITY masquerade is a fleet-wide fingerprint (high)

REALITY's entire security property is that an active prober who connects to the
data port cannot distinguish the node from an ordinary web server. Here
`reality_dest` points at a loopback nginx, so an unauthenticated TLS client is
served by `roles/nginx_mask`. That mask is:

```jinja
{# roles/nginx_mask/templates/index.html.j2 — the whole file #}
{{ mask_body }}
```

where `mask_body` is a single hard-coded bare word (see
[nginx_mask/defaults/main.yml line 10](roles/nginx_mask/defaults/main.yml)).
Grep confirms `mask_body` is **never overridden** — not in the compiled node
plan, not in any playbook, not in desired state. So every node in the fleet
serves a byte-identical response body consisting of one word and no markup.

Two independent consequences, both severe:

1. **The masquerade does not survive a single active probe.** One word of
   `text/html` with no structure is not a plausible website. Anyone who
   TLS-connects to the data port sees immediately that this is not what it
   claims to be — which is exactly the check REALITY exists to defeat.
2. **The whole fleet is enumerable from one response.** Internet-wide scanning
   for hosts serving that exact body on that port returns every node at once.
   Compromising the anonymity of one node compromises all of them
   simultaneously, which is the worst possible correlation property for a VPN.

Fleet enumeration is additionally reachable a second way: the public hostnames
follow a predictable `<region>.<base-domain>` pattern under a single registered
domain, and `dns.proxied` is false (necessarily — REALITY needs direct TCP), so
the domain's records map the fleet without any scanning at all.

Fix: the mask must be a real, plausible, **per-node-distinct** site. This is a
content decision, not only a code change. The minimum bar is a genuine static
site whose body differs per logical node; the strong form is pointing
`reality_dest` at a real third-party host, which is the upstream REALITY
recommendation and removes the self-hosted mask from the threat model entirely.

---

## 4. Host and container hardening

### 4.1 Solid

- **Mode gate with a tripwire.** `roles/common` refuses to touch users, sshd,
  sudoers, firewall, fail2ban, sysctl, auditd or unattended-upgrades in
  `runtime` mode, and *asserts* every flag is false rather than merely ignoring
  them (`tasks/main.yml:12-27`). A runtime deployment structurally cannot alter
  access control.
- **Steady-state reconciliation, not just handlers.** The nftables ruleset is
  re-applied on every hardened run because a handler alone would miss a
  hand-edited kernel table (`tasks/main.yml:~200`). Same reasoning for
  `augenrules --load`. This is the difference between configuration management
  and configuration *convergence*, and the repo gets it right.
- **The bootstrap SSH-port problem is handled correctly.** During bootstrap both
  the default and declared ports stay open, because Ansible reopens its
  connection at unpredictable moments via ControlPersist; steady state renders
  only the declared port. The comment records that this was learned from an
  actual mid-play lockout.
- **nftables replaces only its own table** (`add`+`delete`, never
  `flush ruleset`), leaving Docker's NAT table intact.
- **sysctl hardening deliberately preserves `ip_forward` and omits `rp_filter`**,
  with the reason stated — a naive baseline would break the overlay.
- **fail2ban follows the port sshd is moving to**, not the one the connection
  arrived on.

### 4.2 Finding — SEC-1: container hardening is applied to exactly one container (medium)

Only Vault carries `cap_drop: ["ALL"]` and `security_opt:
["no-new-privileges:true"]`
([platform_vault/templates/compose.yml.j2 lines 25-26](roles/platform_vault/templates/compose.yml.j2)).

No other compose file in the repository sets `cap_drop`, `security_opt`,
`read_only` or `no-new-privileges`. That includes, on internet-facing traffic
nodes:

- **Xray** — `user: "0:0"`, `network_mode: host`, terminating untrusted TLS from
  the public internet on the data port. This is the single highest-risk process
  in the fleet: root, full capability set, host network namespace, directly
  exposed. A container escape here is a host compromise with no intermediate
  step.
- **Alloy** — `user: "0:0"` with `/var/run/docker.sock` mounted. Docker socket
  access is root-equivalent on the host. The hub's copy carries a comment
  acknowledging this; the same mount is placed on every internet-facing node,
  where the acknowledgement does not appear and the exposure is greater.
- **node-exporter** — `pid: host` with `/` bind-mounted read-only.
- **nginx-mask**, **node-agent** — unrestricted, though lower risk.

The asymmetry is the point: the repository demonstrably knows how to constrain a
container and applies it to the one running on the *protected* host, while the
containers on the *exposed* hosts run unconstrained. Xray in particular needs
`NET_BIND_SERVICE` and little else.

### 4.3 Observation — SEC-2: the deploy user is root-equivalent by design

`common_deploy_groups: [sudo, docker]` plus `NOPASSWD:ALL`. The defaults file
states this plainly: "docker group + sudo make it root-equivalent on a Docker
host — accepted deliberately; the win is a named account with no root-password
login, not a hard boundary." Correctly reasoned and correctly documented. Noted
so it is not mistaken for a privilege boundary.

---

## 5. Consistency of sources and flows

### 5.1 Finding — CON-1: readiness smoke adapters are not in Git (high)

`playbooks/operations/readiness.yml` **asserts** that
`spiritvpn_direct_smoke_argv` is non-empty for every exit node (line ~250) and
`spiritvpn_entry_exit_smoke_argv` is non-empty whenever compiled bridges exist.
Both default to `[]`, and the shipped example
([examples/fleet-executor-readiness.yml](examples/fleet-executor-readiness.yml))
ships them empty with the note "Keep empty until reviewed executable probes
exist; readiness then fails closed."

The executor reads the real values from `/etc/spiritvpn/deploy/<env>/readiness.yml`
on the hub — a file that is **not in this repository**. Given that deployments
with an exit node have succeeded, that file has been filled in by hand.

So the definition of "this node actually carries traffic correctly" — the single
most important behavioural check in the system — lives only on the management
host, unversioned, unreviewed, and outside every guarantee this repository
makes. `README.md` forbids exactly this: "Не изменять сервер вручную, если та же
настройка должна принадлежать Git."

This also compounds the known routing gap: an entry→exit smoke test is precisely
what would catch client traffic being blackholed, and it is the check that is
missing from Git.

### 5.2 Finding — CON-2: the repository contradicts itself about which docs are normative (medium)

- `README.md:9-12` — the operator guide is authoritative; everything else in
  `docs/` is reference material that "может не совпадать с текущим `main`".
- `docs/architecture/README.md:3-4` — `INFRA_TECHNICAL_SPEC.md` is "the only
  normative infrastructure architecture document".
- `tests/unit/test_documentation.py:15-21` — enforces the *existence* of
  `INFRA_TECHNICAL_SPEC.md` and `INFRA_V1_IMPLEMENTATION_STATUS.md` under the
  test name `test_normative_v1_documents_exist`.

A passing test asserts these documents are normative while the README says they
are not. The test only checks existence, so it enforces the label without
checking the content — the worst of both.

`docs/status/INFRA_V1_IMPLEMENTATION_STATUS.md` is materially false against
current `main`: dated 15 August 2026, names `feat/infra-v1-foundation` as the
working branch, and states that live rollout has not happened, that environment
directories "пока содержат только объекты окружений", and that end-to-end
workflow execution from `main` is unverified. All four are contradicted by the
deployed fleet, the populated topology files and the deployment ref.

`docs/architecture/TRANSITIONAL_GITHUB_RUNNER.md` describes the GitHub-hosted
runner boundary as temporary; the dedicated runner is live.

Recommend: mark the status document historical with an explicit date banner, or
delete it; rename the test to `test_referenced_documents_exist`; and make
`docs/architecture/README.md` agree with the root README.

### 5.3 Finding — CON-3: `fleetctl/readiness` is tested dead code (medium)

See §3.4. An entire package — `suite.py`, `model.py`, `__init__.py` — plus a
dedicated unit test file, none of it reachable from the CLI or any playbook. It
encodes a *different* and in one respect *better* readiness contract than the
playbook that actually runs (it includes the public-path gate; the playbook does
not), and it omits checks the playbook has (node-agent liveness, metrics bind
enforcement).

Two divergent definitions of readiness, one of which is exercised only by its
own tests. Either wire it in as the source of truth and have the playbook render
from it, or delete it. Leaving it is worse than either: green tests on an unused
model create false confidence, and any future reviewer must first discover it is
dead.

### 5.4 Finding — CON-4: `blackbox_exporter` is a declared component with no implementation (low)

Pinned by digest in `desired/common/components.yml`, propagated into every
compiled node plan, referenced by nothing. See §3.6.

### 5.5 Finding — CON-5: `CODEOWNERS` references a non-existent file (low)

`/playbooks/access.yml` is listed; `playbooks/` contains no such file. The
per-path entries are also redundant — the `*` rule already assigns both owners
to every path, so each subsequent line restates it. Harmless, but it means the
file no longer describes a real ownership structure.

### 5.6 Good consistency worth preserving

- Deterministic render is verified by rendering twice and diffing, in both
  public and trusted CI.
- `_notice: "GENERATED — DO NOT EDIT"` is asserted by consuming roles, not just
  written — a generated file cannot be hand-edited without failing the run.
- `roles/compiled_node_plan` asserts the installed on-host plan matches the
  deployment before mutation, so a stale node fails loudly.
- Secret handling is disciplined: `no_log: true` on secret-bearing tasks,
  failures reported by *reference name* rather than value, and the bridge-UUID
  validator explicitly uses `\Z` rather than `$` because Python's `$` also
  matches before a trailing newline — the exact defect being guarded against.
- `.gitignore` and `.sops.yaml` are coherent; no plaintext secrets, keys or
  decrypted state are tracked. 285 tracked files, all appropriate.

---

## 6. Findings by priority

| ID | Severity | Status | Finding | Anchor |
|---|---|---|---|---|
| ANON-1 | High | open | Identical one-word REALITY mask — fleet enumerable, masquerade defeated | [nginx_mask/defaults/main.yml line 10](roles/nginx_mask/defaults/main.yml) |
| CI-1 | High | **fixed** | `BEFORE` unset — multi-commit pushes silently under-deploy | [desired-state-deploy.yml](.github/workflows/desired-state-deploy.yml) |
| CON-1 | High | open | Readiness smoke adapters live only on the hub, outside Git | [examples/fleet-executor-readiness.yml](examples/fleet-executor-readiness.yml) |
| NET-3 | Medium | open | Public-path reachability gate exists only in dead code | [fleetctl/readiness/suite.py line 19](fleetctl/readiness/suite.py) |
| NET-5 | Medium | open | Compiled probe/health targets are never consumed | [control_observability/tasks/main.yml line 72](roles/control_observability/tasks/main.yml) |
| NET-2 | Medium | **fixed** | ICMP undifferentiated: public echo answered, IPv6 ND rate-limited | [nftables.conf.j2](roles/common/templates/nftables.conf.j2) |
| NET-1 | Medium | **fixed** (firewall); `ListenPort` still pinned | WireGuard port open to the internet with no inbound need | [compiled_node_plan/tasks/main.yml](roles/compiled_node_plan/tasks/main.yml) |
| SEC-1 | Medium | open | No capability restrictions on any internet-facing container | [compiled_runtime/templates/compose.yml.j2](roles/compiled_runtime/templates/compose.yml.j2) |
| CON-2 | Medium | open | Docs contradict each other on what is normative; status doc false | [docs/architecture/README.md line 3](docs/architecture/README.md) |
| CON-3 | Medium | open | `fleetctl/readiness` is tested but unreachable | [fleetctl/readiness/](fleetctl/readiness/) |
| NET-4 | Low | open | Public-port readiness assertion is a substring match | [operations/readiness.yml line 128](playbooks/operations/readiness.yml) |
| NET-6 | Low | open | Exit readiness discloses node addresses to a third party | [compiled_node_plan/defaults/main.yml line 22](roles/compiled_node_plan/defaults/main.yml) |
| CI-2 | Low | open | Unreachable `environment.yml` branch in contour detection | [desired-state-deploy.yml line 376](.github/workflows/desired-state-deploy.yml) |
| CON-4 | Low | open | `blackbox_exporter` declared, never used | `desired/common/components.yml` |
| CON-5 | Low | open | `CODEOWNERS` references a non-existent playbook | [.github/CODEOWNERS](.github/CODEOWNERS) |

Suggested order: **CI-1** (a correctness bug in the deployment trigger, cheap to
fix), then **CON-1 + NET-3 + NET-5 together** — they are one problem seen from
three angles, namely that nothing verifies a node actually works from the
outside. Then NET-2 and NET-1 as a single firewall change, then SEC-1, then the
documentation and dead-code cleanup.

---

## 7. Method and limits

Verified directly: full test suite run (296 pass / 7 skip), `make lint` run
(clean), all eight workflow files read end to end, all firewall/sshd/sysctl
templates read, all compose templates read, `roles/common`,
`roles/compiled_node_plan`, `roles/control_observability`,
`roles/platform_executor`, `roles/platform_wireguard`, `roles/xray`,
`roles/bootstrap_wireguard` read, every playbook read, `scripts/platform-remote.sh`
and the forced-command dispatcher read, dead-code claims confirmed by grep
across the whole tree.

For the three fixes: the rendered nftables ruleset was checked with real `nft`
across five input shapes (traffic node, node bootstrap, management hub, IPv6
echo network, all-empty), then loaded in a network namespace and exercised with
actual packets between two namespaces — overlay ping both directions, public
ping both directions, public TCP to the data port, public TCP to a metrics port.
The detect-step change is covered by the existing behavioural harness plus four
new tests. Nothing was run against a live host.

Inferred, not verified: the live shape of the fleet comes from the local
gitignored `build/develop/` artifacts, which reflect a compile as of 20 August
and may lag `main`. No host was contacted. CON-1 infers that the hub's
`readiness.yml` was hand-filled from the fact that deployments involving an exit
node have succeeded while the shipped example fails closed — worth confirming on
the hub before acting on it.

Not reviewed in depth: `fleetctl/compiler/*` internals beyond their outputs, the
PKI issuance path, Vault policy templates, the Cloudflare DNS adapter, and the
backend manifest gRPC contract.
