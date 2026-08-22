# AGENTS.md — requirements and repository assessment

This file is written for AI agents working on the repository. Part one is the
binding requirements. Part two is an assessment of the actual state: what is
built correctly, what is broken, and which of it has already been fixed.

---

## 1. Requirements

### 1.1. Language

**All documentation and all comments in this repository are written in Russian.**

This covers:

- every `*.md` file in every directory;
- comments in `roles/`, `playbooks/`, `.github/workflows/`, `scripts/`,
  `tests/`, `fleetctl/` — including Python docstrings;
- error messages and `fail_msg` text that an operator reads;
- commit subjects and bodies.

Not translated, left as-is: identifiers, file names, YAML and JSON keys, command
flags, product and protocol names, quotations from external contracts, and
vendored documents. There is no need to mix languages inside a sentence to
translate a term — `nftables`, `conntrack`, `digest`, `overlay` stay as they are.

**This file is the single exception and stays in English.** `AGENTS.md` is not
project documentation; it is an interface addressed to AI agents, and English is
the working language of that interface. Everything it *describes* still follows
the rule above. Do not translate this file, and do not treat its language as
precedent for anything else.

The requirement applies to everything written from the moment it was introduced.
Retroactive translation of what already exists happens as files are edited, not
as one mechanical wave: a three-thousand-line diff that changes no behaviour
cannot be reviewed, and review is precisely the point of the requirement.

Current compliance is tracked in [6.7](#67-language-requirement-compliance).

### 1.2. Documentation and the source of truth

There is deliberately no normative architecture specification in this
repository. A document that has diverged from the code is not a source of
truth — it is worse than no document at all, because people believe it.

- The behaviour of the running system is described by `desired/`, `contracts/`,
  `fleetctl/`, `roles/`, `playbooks/` and the
  [operator guide](docs/operations/INFRA_V1_GUIDE_RU.md).
- A document that cannot be checked against code or a run is not declared
  normative.
- A point-in-time status snapshot with a date in its header goes stale silently.
  Such a document is either updated together with the change it describes, or
  not created at all.

### 1.3. What must never appear in this file

No addresses, domains, port numbers, keys, digests or certificate material —
by the same repository rule that applies everywhere else ("Не коммитить открытые
IP, домены, порты, сертификаты, токены или ключи"). Concrete values are referred
to by their desired-state key path instead.

---

## 2. What this repository is

A GitOps control repository for a VLESS/REALITY VPN fleet. Change flows
Git → management host → servers; observed server state is never written back as
desired state. Three layers:

| Layer | Expressed in | Location |
|---|---|---|
| Desired state | SOPS-encrypted YAML | `desired/` |
| Compiler | Python | `fleetctl/` |
| Application mechanism | Ansible | `roles/`, `playbooks/` |

`fleetctl` compiles the encrypted topology into node plans, an Ansible
inventory, a control plan, monitoring targets, a DNS plan and a backend
manifest. Ansible consumes only compiled artifacts: `roles/compiled_node_plan`
is the single translation point from plan to role inputs, and it refuses to run
against a plan that does not belong to its inventory host
([tasks/main.yml](roles/compiled_node_plan/tasks/main.yml)).

### Verified state

- **Tests**: 304 pass, 7 skipped (`SPIRITVPN_SKIP_LIVE_DESIRED=1`).
- **Lint**: `make lint` clean — ansible-lint 0 failures across 115 files, the
  stricter `production` profile passes.
- **Fleet** (from the gitignored local `build/` artifacts): one management host,
  one entry node, one exit node, a WireGuard management overlay, node images
  pinned by digest.

This repository is well above average. The security model is coherent and mostly
expressed in code rather than prose. The findings below are real, but they sit
on a solid foundation.

---

## 3. CI/CD

### 3.1. What is built correctly

- **Split trust.** Public GitHub runners hold no decryption identity: they
  validate SOPS envelope structure and compile a plaintext fixture. Only the
  dedicated runner with a local age identity opens the real topology
  (`ci.yml`, job `trusted-desired-state`).
- **Exact-SHA handoff.** No deployment picks a "latest available" commit:
  `git rev-parse HEAD` is asserted equal to the requested SHA, and reachability
  from `origin/main` is asserted
  ([fleet-deploy.yml](.github/workflows/fleet-deploy.yml)).
- **Credentials never cross the boundary.** The runner hands a Git bundle over
  stdin to a forced SSH command. `scripts/platform-remote.sh` validates every
  argument with anchored regexes, `authorized_keys` binds each key to one
  environment via `restrict,command=`
  ([authorized_keys.j2](roles/platform_executor/templates/authorized_keys.j2)),
  and `spiritvpn-github-command` revalidates everything server-side. Two
  independent checks — not duplication, but two different trust boundaries.
- **Write access is isolated.** `contents: write` lives in the single `promote`
  job, which has no SSH key, no bundle and no hub access. The ref moves by
  compare-and-swap on both ends.
- **A zero exit code is not treated as success.** `deployment-record.py` parses
  the executor transcript to decide whether the backend actually accepted the
  manifest before the ref may advance.
- **Actions are pinned by commit SHA**, with the version in a trailing comment.
- **`prod` is excluded from the automatic path**, with the reason recorded at
  the point of decision.

### 3.2. CI-1: multi-commit pushes were under-deployed — **fixed**

`BEFORE` was never set, so CI always took the default branch that substituted
the parent of the head commit. A push of several commits was analysed as if only
the last one existed: the contours of the rest were never deployed, and the run
stayed green. Exactly the silent under-deployment the step exists to prevent.

The baseline is now the head commit of the last *successful* run of this same
workflow, and `split` refuses to run without one. One push produces one CI run
and one reconcile run, so that commit is precisely the state before the current
push; the delta of a failed run carries into the next comparison. With no
successful run in history the baseline is the empty tree — every contour
reconciles rather than guessing at the unknown.

Why the defect survived a dedicated test suite is worth recording:
`tests/unit/test_desired_state_detect.py` executes the real step script but
**injects `BEFORE` itself**. Production took the default branch; the tests never
did. Both sides were correct about the code they could see.

The first version of the fix had a defect of its own: the API call sat inside a
process substitution, whose exit status is invisible to both `set -e` and
`pipefail`. A transient network error would have read as "no successful
reconcile exists" and triggered a full deployment of every contour. The query is
now a separate command whose failure stops the step.

Locked in by eight tests: multi-commit push coverage, a snapshot of the old
behaviour as a defect, two wiring guards, and four on the baseline step itself
(reachable run, commit lost to force-push, empty history, failing query).

### 3.3. CI-2: unreachable branch in contour detection

The `*/environment.yml` branch
([desired-state-deploy.yml](.github/workflows/desired-state-deploy.yml)) splits
control from fleet by comparing `.spec.control` subtrees with `yq`. No
`environment.yml` exists under `desired/environments/*/` — only
`topology.sops.yml`. The branch is unreachable in production and silently
depends on `yq` being present on the runner image. In tests it is covered: the
fixture creates one. Kept deliberately — deleting it would make a future
`environment.yml` visible to fleet only, i.e. introduce a new under-deployment.

### 3.4. CI-3: the release trust chain reaches outside this repository

`release-bump.yml` runs on `repository_dispatch` **on the dedicated runner**,
decrypts the topology and pushes straight to `main` with `INFRA_PUSH_TOKEN`,
bypassing pull request and code-owner review. For `develop` the resulting commit
then deploys automatically.

The decision is deliberate and documented, and the blast radius is bounded:
every payload field is validated with anchored regexes, and the
`Refuse an unexpected change` step fails the job if the diff touches anything
but the single topology file. What remains worth stating plainly: **any
repository or token able to send a `repository_dispatch` here can move a running
image digest in `develop` with no human involved.** That is an accepted risk,
not a defect — but it should be accepted explicitly, and the token's scope and
rotation belong in the operator guide.

### 3.5. CI-4: environment separation rests on SSH, not on GitHub

`environment:` is absent from every deployment job because GitHub Environments
are unavailable on the current plan. The workflows explain this at length, and
the actual separation — a forced command bound to one environment plus
server-side revalidation — works. The consequence: **there is no approval gate
anywhere.** `prod` is protected only by being filtered out of the automatic path
and requiring a manual dispatch. Whoever can dispatch a workflow can apply
`prod`. Re-examine when the plan changes.

### 3.6. Minor

`platform-readiness.yml` checks out the default branch with no pinned `ref:`,
unlike every other workflow. Low impact — the step runs only a read-only remote
command — but inconsistent.

---

## 4. Topology, ports and network surface

### 4.1. Bind discipline is the strongest part of the repository

Exposure is decided in one place
([compiled_node_plan/tasks/main.yml](roles/compiled_node_plan/tasks/main.yml))
and then held by three independent layers:

1. **Bind address.** Every observability listener is bound to the node's overlay
   address; Vault, the Xray gRPC API, its metrics and the Alloy UI are
   loopback-only. A wildcard is used nowhere.
2. **Firewall.** `common_restricted_tcp_rules` additionally scopes the agent and
   metrics ports to the management network and the overlay interface — with a
   comment stating outright that the rule exists so the firewall is not the only
   thing between `/metrics` and the internet if a bind is ever loosened.
3. **Readiness.** The playbook re-asserts the bind and **fails** on a wildcard
   rather than warning
   ([operations/readiness.yml](playbooks/operations/readiness.yml)).

Three layers, each of which independently prevents the same disclosure, with the
reasoning recorded. This is the right pattern, applied consistently.

Public ingress on a node is limited to the declared data port, SSH from the
management network, and — before the fix below — the WireGuard port. Xray
routing blackholes `geoip:private` ahead of everything else, which stops a
customer from reaching the operator's private networks through an exit node.

### 4.2. NET-1: the WireGuard port was open to the internet needlessly — **fixed**

The node brings up the tunnel: its config carries the hub's `Endpoint` and
`PersistentKeepalive`, while the hub's peer entry for the node has no endpoint
at all — the address is learned from the incoming handshake
([configure-wireguard.sh.j2](roles/bootstrap_wireguard/templates/configure-wireguard.sh.j2),
[base.conf.j2](roles/platform_wireguard/templates/base.conf.j2)). A node needs
no inbound listener, so the open port was pure attack surface and a fleet
signal: the same well-known UDP port on every node whose public role is to look
like an ordinary web server.

`common_public_udp_ports` is now empty for nodes. Existing tunnels are not
broken: the hub's replies arrive on the conntrack entry created by the node's
own outbound packet, and keepalive is shorter than that entry's timeout.

**Still open:** `ListenPort` remains pinned to the same value on every node and
is therefore the tunnel's *source* port — visible to any on-path observer and
uniform across the fleet. Unpinning it restarts the interface on live nodes, so
it belongs in a separate deliberate change.

### 4.3. NET-2: ICMP was handled as a single class — **fixed**

**Correction to the first revision of this document.** The ICMP rules sit
*after* `ct state established,related accept`. Conntrack marks ICMP errors
belonging to a tracked flow as `related`, so `fragmentation-needed` and
`packet-too-big` for existing connections are accepted before the limiter. Path
MTU Discovery for tracked flows was never broken — the original claim was too
strong.

The shared 20/s bucket governed the ICMP that conntrack sees as `NEW`. That left
two genuine problems:

1. **IPv6 Neighbour Discovery was rate-limited.** NS/NA/RS/RA arrive as `NEW`
   and fell under the limit, and without them IPv6 does not work on the link at
   all. A single sender at 20+ pps of any ICMPv6 would starve ND for the whole
   interface. IPv6 is not declared in desired state, so the problem was latent —
   but its failure mode (intermittent, load-dependent connectivity loss) is
   among the hardest to attribute.
2. **Echo was accepted from anywhere.** Every node answered ping from the whole
   internet, confirming its own existence.

There are now four classes: ICMP/ICMPv6 errors accepted unconditionally, ND and
MLD accepted unconditionally and unlimited, echo accepted only from
`common_icmp_echo_cidrs` at 5/s, and everything else dropped silently — a reject
would confirm the host as well as a reply. Timestamp goes with echo, closing a
clock-correlation channel. The `output` chain gained the second direction:
echo-request leaves only toward trusted interfaces and declared networks. Error
signalling still leaves the host, deliberately — suppressing it would break
PMTUD for clients connecting to the data port.

The only live ICMP dependency is the hub reachability check in
`bootstrap_wireguard`, a ping from the node to the hub's overlay address. The
hub accepts it via `iifname "<overlay>" accept` (the hub sets
`common_trusted_interfaces`; nodes do not), not via the general ICMP rule, so
closing public echo does not break bootstrap.

Verified in network namespaces against the rendered ruleset rather than by
reading: overlay ping works both directions, public ping is unanswered inbound
and dropped outbound, the data port stays reachable, the metrics port stays
closed.

### 4.4. NET-3: the public-path gate exists only in dead code

`fleetctl/readiness/suite.py` defines two reachability gates — `host_reachable`
against the **public** address and `management_address_reachable` against the
overlay one. That is a correct two-path model: it distinguishes "the machine is
up" from "the machine is up and the overlay converged".

**Nothing calls it.** `build_gate_specs` and `GateRunner` are imported only by
`tests/unit/test_readiness.py`. Neither `fleetctl/cli.py` nor any playbook
references the module; the `ReadinessProbe` protocol has no implementation.

The readiness that actually runs is `playbooks/operations/readiness.yml`, and it
checks the overlay path only. The public path is never verified from outside.
Together with [4.6](#46-net-5-compiled-probes-are-read-by-nobody) this means:
**no automated mechanism confirms that a node is reachable from the internet.** A
node with a broken public interface, DNS record or listener passes readiness and
is declared serving.

### 4.5. NET-4: the public-port readiness assertion is a substring match

```yaml
- "(':' ~ (…public.port | string)) in _readiness_listeners.stdout"
```

This searches the whole `ss -H -lnt` output for a substring. It passes if
anything listens on a port whose decimal form starts with those digits, on
**any** address — including loopback. The contrast with the metrics assertion
twenty lines below, which matches `address:port` exactly and explicitly rejects
wildcards, shows the strict form was known to the author.

### 4.6. NET-5: compiled probes are read by nobody

`fleetctl` compiles targets in three collections, all marked
`readiness_expected`:

| Collection | Kind | Consumer |
|---|---|---|
| `management` | `metrics` | Prometheus via file_sd |
| `management` | `health` (agent gRPC) | **nobody** |
| `external` | `probe` (public port) | **nobody** |
| `node-local` | `metrics` (Xray) | **nobody** |

`roles/control_observability` selects `kind == 'metrics'` and
`collection == 'management'` and discards the rest. There is no prober job in
the Prometheus skeleton. `blackbox_exporter` is pinned by digest in
`desired/common/components.yml` and reaches every node plan, but **is referenced
by no role, playbook, template or compose file** — it exists only in generated
artifacts.

So the external probe of the public port — the one thing that would independently
detect the gap in [4.4](#44-net-3-the-public-path-gate-exists-only-in-dead-code)
— is compiled, declared readiness-expected, and silently dropped. A declared
probe that never runs is worse than none: it reads as coverage.

### 4.7. NET-6: exit readiness discloses addresses to a third party

`spiritvpn_smoke_echo_url` points at a public echo service
([compiled_node_plan/defaults/main.yml](roles/compiled_node_plan/defaults/main.yml)).
The trade-off is acknowledged in a comment, and the check itself is meaningful —
it proves egress is neither proxied nor NATed. But for a VPN operator it means
every readiness run reports the fleet's exit addresses, correlated in time, to a
third-party service with its own logs. For `prod` this deserves an endpoint on
infrastructure already being maintained.

### 4.8. ANON-1: the REALITY mask is a fleet-wide fingerprint

REALITY's entire protection rests on an active prober being unable to
distinguish the node from an ordinary web server when it connects to the data
port. `reality_dest` points at a local nginx, and that mask site is one template:

```jinja
{# roles/nginx_mask/templates/index.html.j2 — the whole file #}
{{ mask_body }}
```

where `mask_body` is a single hard-coded bare word
([nginx_mask/defaults/main.yml](roles/nginx_mask/defaults/main.yml)). Grep
confirms `mask_body` is **overridden nowhere** — not in the node plan, not in a
playbook, not in desired state. Every node in the fleet returns a byte-identical
one-word response with no markup.

#### Why the mask is the whole of the protection

A client sends a ClientHello whose SNI is one of `serverNames`, carrying auth
data derived from its X25519 keypair against the server's public key plus a
`shortId`. Xray intercepts it and opens a real TLS session to `dest`.

- **Auth verifies** → Xray hijacks the session after `dest` returns its
  certificate and switches to the VLESS tunnel.
- **Auth fails** → Xray transparently proxies the whole connection to `dest`.
  The prober gets a complete, genuine TLS session with whatever `dest` serves.

REALITY therefore hides nothing from a prober; it **redirects the prober to a
real website**. The strength of the camouflage equals the plausibility of
`dest`, and nothing else.

#### The architecture is right; the content is not

The self-hosted mask is a *better* choice than the usual third-party `dest`, and
this should not be "fixed" by switching to one. The standard weakness of a
third-party `dest` is a contradiction between DNS and observation: public DNS
says the site lives at some CDN, yet this IP serves it. The self-hosted variant
has no such contradiction — DNS says the domain lives at this IP, and this IP
serves that domain under a valid certificate for it. Keep the architecture.

#### Failure modes

1. **It is not a website.** Roughly seven bytes of `text/html`: no `<html>`, no
   `<title>`, no CSS, no favicon, no links, no assets. This is worse than a
   parked domain, which at least has a registrar page. A prober learns something
   specific: this IP holds a valid certificate for a real domain and serves a
   placeholder behind it.
2. **It is identical fleet-wide.** The body hash is a fleet fingerprint. One
   query enumerates every node at once, and no scanning is required — public
   scan datasets already index responses on the data port continuously. It is
   **retroactive** (historical scan data exposes nodes already rotated away
   from) and **automatic** (a new node joins the fingerprint the moment it
   deploys, so rotation buys nothing).
3. **The HTTP port is closed.** `common_public_tcp_ports` carries only the data
   port. Real websites answer on the plain-HTTP port with a redirect. Valid
   certificate, real DNS, HTTPS-only, no redirect port, seven-byte body — each
   element is weak alone and they compound.
4. **The header combination is distinctive.** `server_tokens off`,
   `ssl_session_tickets off`, plus `X-Content-Type-Options` and
   `Referrer-Policy` on a seven-byte placeholder. Real placeholders do not ship
   hardened headers; real hardened sites do not ship seven-byte bodies.

Nobody caught this because nothing probes the public port from outside
([4.4](#44-net-3-the-public-path-gate-exists-only-in-dead-code),
[4.6](#46-net-5-compiled-probes-are-read-by-nobody)). No automated check has
ever looked at what a node serves to the internet.

#### Two enumeration paths that need no scanning

- **DNS.** Public hostnames follow a predictable pattern under one registered
  apex, and proxying is off — necessarily, REALITY needs direct TCP. Knowing one
  hostname lets an observer guess the siblings; passive DNS datasets already
  hold them.
- **Certificate Transparency.** Every certificate for the apex and its
  subdomains is publicly and permanently logged. CT alone yields the list of
  logical nodes, historically, without touching the infrastructure.

#### Planned direction (decided, not yet implemented)

Per-logical-node mask content shipped as an **OCI artifact pinned by digest**,
built in its own repository and pulled at deploy. This matches how every other
component is already pinned and moves site content — which inherently contains
the domain — out of this repository.

The artifact carries **only static files**, not nginx. The nginx image stays
pinned centrally in `components`, the TLS configuration stays owned by the role,
and only content varies per node.

**Prerequisite that does not exist today.** There is **no registry
authentication anywhere in this repository** — a tree-wide grep for
`docker login`, `DOCKER_CONFIG`, `"auths"` and equivalents returns nothing, and
images are pulled by `docker compose --pull` with no credentials. Nodes
nonetheless pull `node_agent` from a private-looking namespace, so either that
package is public or credentials were placed on the hosts by hand outside Git —
the latter being a CON-1-class problem in its own right. Establishing
Git-expressed private registry access on nodes is step one; without it this plan
publishes the camouflage.

**Three traps that would negate the work:**

- **Public artifacts destroy the camouflage completely**, and leave the fleet
  worse off than today: anyone could pull every site and obtain a ready-made
  fingerprint set without touching the infrastructure.
- **Repository names must be opaque.** A name encoding the node (`mask-<node>`)
  gives away the image→node mapping in a single package listing.
- **One artifact for the whole fleet is not acceptable**, however much simpler
  the release flow would be: seizing one node would then expose every mask and
  restore exactly the correlation this work removes. The artifact is per logical
  node.

**Moving parts:**

| Where | Change |
|---|---|
| new repository | builds one static-content image per logical node |
| `contracts/desired-state/logical-node.schema.json` | `mask` gains `site` = `{repository, digest}`, **optional** |
| `fleetctl/compiler/node_plans.py` | propagates `site` into the node plan |
| `roles/compiled_node_plan` | asserts the digest form, as it already does for other components |
| `roles/nginx_mask` | extracts the artifact into the site directory when `site` is set; otherwise renders the current template |
| `roles/compiled_node_plan` | opens the plain-HTTP port; `roles/nginx_mask` adds a redirect server block |
| `release-bump.yml`, `scripts/topology-release.py` | new `mask-release` type addressing a path **inside** a logical node, which no existing bump does |

`site` must be **optional**. A new required field fails the impact plan against
the previous deployment baseline — the schema lesson already learned here.
Absence of `site` keeps current behaviour, which also makes migration
node-by-node.

**The real cost is content**, not machinery: N plausible, *structurally* distinct
sites, growing with the fleet. One template with a swapped variable is what
exists today and must not be recreated — identical structure re-correlates under
structural hashing even with different text. Decide where sites come from before
starting, or the shortcut returns within months.

**Honest limit — what this does not fix.** The mask defeats *mass scanning*
(finding nodes by content without knowing the domain) and *active probing*
(confirming a suspicion). That is the main censor workflow, so the gain is real.
It does **not** address enumeration via CT and DNS: whoever learns the apex
domain obtains the node list regardless of what the nodes serve. **The domain is
therefore the weaker link, and mask content is secondary to it.** Fixing the
mask while leaving one apex with predictable subdomains removes the cheapest and
most automatic enumeration path but leaves a single point that yields the whole
fleet. Options are a wildcard certificate (one CT entry instead of one per node,
at the cost of a shared key) or separate apexes per logical node (more expensive,
but it breaks the linkage). Plan both, or the work buys less than it costs.

**Sequence:**

1. Git-expressed private registry access from nodes — without it the rest
   publishes the camouflage.
2. Domain strategy decision (wildcard vs separate apexes) — it determines how
   many distinct sites are actually needed.
3. Optional `site` in the schema, compiler, role, plain-HTTP redirect port.
4. Site repository and `mask-release`.
5. The external probe asserts that what is served matches what Git declares —
   the same work as [4.6](#46-net-5-compiled-probes-are-read-by-nobody), which
   is why they belong together: until then none of steps 1–4 can be confirmed.

---

## 5. Host and container hardening

### 5.1. Done well

- **Mode gate with a tripwire.** `roles/common` refuses to touch users, sshd,
  sudoers, firewall, fail2ban, sysctl, auditd or unattended upgrades in
  `runtime` mode, and **asserts** that every flag is off rather than merely
  ignoring them. A runtime deployment structurally cannot change access control.
- **Reconciliation, not just handlers.** The nftables ruleset is re-applied on
  every hardened run, because a handler would miss a kernel table edited by
  hand. Same reasoning for `augenrules --load`. This is the difference between
  configuration management and configuration convergence.
- **The bootstrap SSH port problem is solved correctly.** During bootstrap both
  the default and declared ports stay open, because Ansible reopens its
  connection at an unpredictable moment; steady state leaves only the declared
  one. The comment records that this was learned from a live disconnect.
- **nftables replaces only its own table** (`add`+`delete`, never
  `flush ruleset`), so Docker's NAT table is untouched.
- **sysctl deliberately preserves `ip_forward` and leaves `rp_filter` alone** —
  with the reason recorded: a naive baseline would break the overlay.

### 5.2. SEC-1: capability restrictions applied to exactly one container

Only Vault carries `cap_drop: ["ALL"]` and
`security_opt: ["no-new-privileges:true"]`
([platform_vault](roles/platform_vault/templates/compose.yml.j2)). No other
compose file in the repository sets `cap_drop`, `security_opt`, `read_only` or
`no-new-privileges`. That includes, on internet-facing nodes:

- **Xray** — `user: "0:0"`, `network_mode: host`, terminating untrusted TLS from
  the internet on the data port. The riskiest process in the fleet: root, full
  capability set, host network namespace, direct exposure. A container escape
  here is a host compromise with no intermediate step. It needs roughly
  `NET_BIND_SERVICE` and little else.
- **Alloy** — `user: "0:0"` with the Docker socket mounted, which is
  root-equivalent on the host. On the hub this is acknowledged in a comment; on
  nodes the same mount sits without one, and the exposure there is greater.
- **node-exporter** — `pid: host` with `/` mounted read-only.

The asymmetry is the finding: the repository knows how to constrain a container
and applies it to the one running on the *protected* host, while the containers
on the *exposed* hosts run unconstrained.

### 5.3. SEC-2: the deploy account is root-equivalent by design

`common_deploy_groups: [sudo, docker]` plus `NOPASSWD:ALL`. The defaults file
says so plainly: the win is a named account with no password login, not a hard
boundary. The reasoning is correct and recorded. Noted so it is not mistaken for
a privilege boundary.

---

## 6. Consistency of sources and flows

### 6.1. CON-1: readiness smoke adapters are not in Git

`playbooks/operations/readiness.yml` **asserts** that
`spiritvpn_direct_smoke_argv` is non-empty for every exit node and
`spiritvpn_entry_exit_smoke_argv` whenever compiled bridges exist. Both default
to empty, and the shipped example
([examples/fleet-executor-readiness.yml](examples/fleet-executor-readiness.yml))
ships empty with the note to keep it so until reviewed executable probes exist,
readiness failing closed in the meantime.

The executor reads the real values from
`/etc/spiritvpn/deploy/<env>/readiness.yml` on the hub — a file **not present in
this repository**. Since deployments involving an exit node have succeeded, that
file has been filled in by hand.

So the definition of "this node actually carries traffic correctly" — the most
important behavioural check in the system — lives only on the management host,
unversioned, unreviewed, and outside every guarantee this repository makes.
`README.md` forbids exactly this: "Не изменять сервер вручную, если та же
настройка должна принадлежать Git".

### 6.2. CON-2: the documents contradicted each other — **fixed**

Previously: the root `README.md` called every document in `docs/` reference
material, while `docs/architecture/README.md` called `INFRA_TECHNICAL_SPEC.md`
"the only normative" one, and `tests/unit/test_documentation.py` enforced
normative status for it and for the implementation-status snapshot — while
checking only that the file existed. A passing test asserted a status whose
content it verified in no way. The status snapshot was materially false: its
date, branch, claim of no live rollout and claim of empty environment
directories all diverged from `main`.

The specification and the snapshot were removed from `main` (the full text
remains in Git history), `docs/status/` was deleted entirely,
`docs/architecture/README.md` was rewritten, and the test was renamed to
`test_referenced_documents_exist` and now checks what contracts and procedures
actually reference.

Sections 23 and 24 of the specification — open decisions and the backend delta —
were **not** stale: section 24 describes the live manifest boundary, the
caller-identity environment check and the frozen proto baseline, and the backend
contract re-vendoring procedure points at them. They are preserved in
[`contracts/backend/INFRA_DELTA.md`](contracts/backend/INFRA_DELTA.md) next to
what uses them, with the references repointed.

### 6.3. CON-3: `fleetctl/readiness` is tested dead code

See [4.4](#44-net-3-the-public-path-gate-exists-only-in-dead-code). A whole
package plus a dedicated test file, unreachable from the CLI and from every
playbook. It encodes a **different** and in one respect **better** readiness
contract than the playbook that runs (it includes the public-path gate the
playbook lacks) and omits checks the playbook has.

Two divergent definitions of readiness, one exercised only by its own tests.
Either wire it in as the source of truth and generate the playbook from it, or
delete it. Leaving it is worse than either: green tests on an unused model
create false confidence.

### 6.4. CON-4: `blackbox_exporter` is declared and unused

Pinned by digest, reaches every node plan, referenced nowhere. See
[4.6](#46-net-5-compiled-probes-are-read-by-nobody).

### 6.5. CON-5: `CODEOWNERS` references a file that does not exist

`/playbooks/access.yml` is listed; `playbooks/` contains no such file. The
per-path entries are redundant anyway: the `*` rule already assigns both owners
to everything, and each following line restates it. Harmless, but the file no
longer describes a real ownership structure.

### 6.6. Good practice worth preserving

- Deterministic rendering is verified by rendering twice and diffing, in both
  public and trusted CI.
- `_notice: "GENERATED — DO NOT EDIT"` is asserted by consumers, not merely
  written: a generated file cannot be hand-edited without failing the run.
- `roles/compiled_node_plan` compares the plan installed on the host with the
  deployment's plan before any mutation, so a stale node fails loudly.
- Secret handling is disciplined: `no_log` on secret-bearing tasks, failures
  reported by *reference name* rather than value, and the bridge UUID check
  using `\Z` rather than `$` — because in Python `$` also matches before a
  trailing newline, which is the exact defect being guarded against.
- `.gitignore` and `.sops.yaml` are coherent; no plaintext secrets, keys or
  decrypted state are tracked.

### 6.7. Language requirement compliance

The requirement in [1.1](#11-language) was introduced by this document; existing
material does not yet comply. Measurement at the time of introduction:

| Surface | Non-Russian comment lines |
|---|---|
| `fleetctl/` (including docstrings) | 209 |
| `roles/` | 510 |
| `tests/` | 167 |
| `scripts/`, `playbooks/`, workflows | 77 |

Fully English documents: `docs/README.md`, `docs/integration/README.md`,
`docs/operations/SELF_HOSTED_RUNNER.md`, `docs/operations/PLATFORM_BOOTSTRAP.md`,
`docs/architecture/TRANSITIONAL_GITHUB_RUNNER.md`, `contracts/README.md`,
`contracts/manifest/README.md`, `contracts/desired-state/README.md`,
`desired/README.md`, `roles/README.md`, `fleetctl/README.md`.

These are brought into line as each file is next edited — see the reasoning in
[1.1](#11-language) for why not in one wave. This file is exempt by
[1.1](#11-language) and is not counted above.

---

## 7. Findings by priority

| ID | Severity | Status | Finding |
|---|---|---|---|
| ANON-1 | high | open; direction chosen ([4.8](#48-anon-1-the-reality-mask-is-a-fleet-wide-fingerprint)) | Identical REALITY mask: the fleet is enumerable and the mask survives no active probe |
| CON-1 | high | open | Readiness smoke adapters live only on the hub, outside Git |
| CI-1 | high | **fixed** | `BEFORE` unset: multi-commit pushes were under-deployed |
| NET-3 | medium | open | The public-path gate exists only in dead code |
| NET-5 | medium | open | Compiled probes and health targets are read by nobody |
| SEC-1 | medium | open | No internet-facing container has capability restrictions |
| CON-3 | medium | open | `fleetctl/readiness` is tested and unreachable |
| NET-2 | medium | **fixed** | ICMP as one class: public echo answered, IPv6 ND rate-limited |
| NET-1 | medium | **fixed** (firewall); `ListenPort` pinned | WireGuard port open to the internet needlessly |
| CON-2 | medium | **fixed** | Documents contradicted each other; the status snapshot was false |
| NET-4 | low | open | The public-port readiness assertion is a substring match |
| NET-6 | low | open | Exit readiness discloses addresses to a third party |
| CI-2 | low | open | Unreachable `environment.yml` branch |
| CI-3 | low | accepted | Release dispatch moves a digest without review |
| CI-4 | low | accepted | No approval gate; `prod` is protected only by manual dispatch |
| CON-4 | low | open | `blackbox_exporter` declared, unused |
| CON-5 | low | open | `CODEOWNERS` references a non-existent playbook |

A sensible order from here: **ANON-1** (largest impact, but needs a decision
about mask content), then **CON-1 + NET-3 + NET-5 together** — one problem seen
from three angles, namely that nothing verifies a node from outside. Then
**SEC-1**, then the small items and the dead-code cleanup.

---

## 8. Method and limits

Verified directly: full test and lint runs; all eight workflows read end to end;
the firewall, sshd and sysctl templates read; every compose file read; the
`common`, `compiled_node_plan`, `control_observability`, `platform_executor`,
`platform_wireguard`, `xray` and `bootstrap_wireguard` roles read; every
playbook read; `scripts/platform-remote.sh` and the forced-command dispatcher
read; dead-code claims confirmed by grep across the whole tree.

For the fixes: the rendered nftables ruleset was checked with real `nft` across
five input shapes, then loaded into a network namespace and exercised with real
packets between two namespaces — overlay ping both directions, public ping both
directions, public TCP to the data port and to the metrics port. The contour
detection change is covered by the existing behavioural harness plus eight new
tests. Nothing was executed against a live host.

Inferred, not verified: the fleet composition comes from the local `build/`
artifacts, which may lag `main`. No server was queried. The assumption in
[6.1](#61-con-1-readiness-smoke-adapters-are-not-in-git) that the hub's
readiness file was filled in by hand follows from deployments with an exit node
having succeeded while the shipped example fails closed — worth confirming on
the hub before acting on it.

Not examined in depth: the internals of `fleetctl/compiler/*` beyond their
outputs, the PKI issuance path, the Vault policy templates, the Cloudflare DNS
adapter and the gRPC manifest contract.
