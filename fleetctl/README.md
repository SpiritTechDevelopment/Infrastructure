# fleetctl

`fleetctl` is the infrastructure v1 desired-state compiler and deployment
coordinator. Model loading, validation, compilation, planning, and dry-run
deployment are local and deterministic. External actions are isolated in
adapters, and Ansible is invoked only when deployment receives explicit
`--apply` authorization.

Implemented commands:

```bash
fleetctl validate --environment develop
fleetctl render --environment develop --output build/develop
fleetctl plan --environment develop --source HEAD --output build/develop
fleetctl ansible-check --environment develop --build-dir build/develop
fleetctl provisioning-check --environment develop
fleetctl deploy --environment develop --source HEAD
```

`plan` resolves its normal baseline from
`refs/deployments/<environment>`. A missing ref fails closed unless the first
deployment is explicitly declared with `--initial`. The source and baseline are
read directly from Git commit trees without checkout or reset, and dirty or
untracked `desired/` input cannot be attributed to a Git SHA. An explicit
`--baseline <desired-directory>` remains available only for tests.

The current render produces:

```text
build/<environment>/
├── ansible-inventory.json
├── bootstrap-inventory.json
├── dns-plan.json
├── monitoring-targets.json
└── node-plans/<instance_id>.json
```

`plan` additionally writes `impact-plan.json`. Generated Ansible inventories
contain connection data and references to compiled node plans; the deploy
playbooks do not read `desired/` or the legacy manual production inventory.

The deployment coordinator is resume-safe and dry-run by default. Its ordered
infrastructure stages are:

```text
validate → resolve Git baseline → impact plan → manual provisioning preflight
         → allocate/pin manifest revision → render/Ansible input validation
         → bootstrap → configure → readiness
```

In dry-run mode, bootstrap, configure, and readiness are recorded as
`SKIPPED_DRY_RUN`, so no SSH or mutation occurs. With explicit `--apply`, all
three readable operator-input files are required:

```bash
fleetctl deploy --environment develop --source HEAD --apply \
  --bootstrap-vars /protected/bootstrap.yml \
  --compiled-secrets /protected/compiled-secrets.yml \
  --readiness-vars /protected/readiness.yml
```

Even after successful infrastructure apply, the coordinator stops at
`WAITING_FOR_BACKEND`. Backend manifest apply, DNS/data-plane promotion, and
`refs/deployments/*` updates are not implemented by the coordinator and are
never reported as complete. `update-deployment-ref` exists as a separate atomic
compare-and-swap primitive for a future fully verified deployment flow; current
deployment code does not call it.

The coordinator now keeps environment-scoped revision allocations and records
under `.fleetctl-state/` by default. A deployment receives one
revision, and resume must reproduce the same payload digest, rendered-byte hash,
size, and destructive flag. Lost or conflicting state fails closed. The rendered
`backend-manifest.json` is still ephemeral; the durable record stores only its
identity and hashes. Dry-runs allocate revisions too, so gaps are normal and
safe. On the management executor, pass
`--state-dir /var/lib/spiritvpn/fleetctl` (or `FLEET_STATE_DIR` through Make) and
include that root-owned directory in backup before a real backend adapter is
enabled. It never belongs on the GitHub-hosted runner.

The pinned `spiritvpn.manifest.v1` contract and a pure full-snapshot compiler now
exist. The compiler requires a matching impact plan, a positive uint64 revision,
and exact destructive permission; it emits no secret references and performs no
RPC. `APPLIED` and `IDEMPOTENT` are the future successful deployment boundary.
Backend materialization remains asynchronous and must be monitored through its
operation/materialization metrics and alerts.

An independent offline review artifact can also be rendered with an explicit
revision, without changing coordinator allocation state or accessing backend:

```bash
make fleet-manifest ENVIRONMENT=develop SOURCE=HEAD INITIAL=1 REVISION=1
```

For a destructive impact plan, `ALLOW_DESTRUCTIVE=1` is additionally required;
the same flag is refused when the plan is non-destructive.

Node plans are separate infrastructure topology projections, not backend
manifests or `infraagent.v1` requests. They contain secret references, never
resolved secret values.

## Manual platform bootstrap

The first management host uses the one hand-maintained inventory allowed by the
v1 specification: `inventories/bootstrap/platform.yml`. It must contain exactly
one global address and the `root` bootstrap user. The complete independently
verified public host key lives in `inventories/bootstrap/known_hosts`.

```bash
make fleet-platform-check
make fleet-platform-bootstrap APPLY=1 \
  PLATFORM_VARS=/protected/platform-bootstrap.yml
```

The apply target installs loopback-only Vault with host-generated transport TLS,
hardens the host and installs a separate `github-deploy` account. That account's
SSH keys are restricted to a root-owned command gate; today the only accepted
operation is read-only `platform-readiness`. GitHub stores only its private SSH
key and the host as an environment variable. Vault credentials and resolved
secrets never enter the hosted runner.

Bootstrap never runs `vault operator init`, unseals Vault, stores recovery
material, writes secrets, or moves a deployment ref. The operator ceremony and
future local Vault resolver are separate handoff stages.
