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
fleetctl check-change --environment develop --source HEAD --output build/develop
fleetctl ansible-check --environment develop --build-dir build/develop
fleetctl provisioning-check --environment develop
fleetctl dns --environment develop --token-file /protected/cloudflare-token
fleetctl deploy --environment develop --source HEAD
```

`dns` сверяет с Cloudflare все `serving` entry- и exit-endpoint из
`dns-plan.json`. По умолчанию команда только показывает `create`/`update`;
изменение записей требует явного `--apply`. Адаптер не удаляет записи, принимает
только `A`/`AAAA` внутри объявленной зоны и всегда требует DNS-only режим. Файл
API-токена должен быть обычным файлом без доступа для group/world (например,
mode `0600`). Эквивалентные Make-цели — `fleet-dns-plan` и
`fleet-dns-apply APPLY=1` с `CLOUDFLARE_TOKEN_FILE=...`.

`plan` resolves its normal baseline from
`refs/deployments/<environment>`. A missing ref fails closed unless the first
deployment is explicitly declared with `--initial`. The source and baseline are
read directly from Git commit trees without checkout or reset, and dirty or
untracked `desired/` input cannot be attributed to a Git SHA. An explicit
`--baseline <desired-directory>` remains available only for tests.

`check-change` is the CI entry point for an ordinary fleet edit. It always uses
that deployment ref (or an explicit `--initial`), validates the source, renders
all artifacts, validates the generated Ansible inventory and prints a compact
transition summary (`addition`, `replacement`, `modification`, `removal` or
`no-op`). Tests do not duplicate live node IDs, addresses or fleet sizes.

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
playbooks do not read `desired/` or any hand-maintained fleet inventory.

The deployment coordinator is resume-safe and dry-run by default. Its ordered
infrastructure stages are:

```text
validate → resolve Git baseline → impact plan → manual provisioning preflight
         → allocate/pin manifest revision → render/Ansible input validation
         → bootstrap → configure → readiness → backend manifest → DNS
```

In dry-run mode, bootstrap, configure, and readiness are recorded as
`SKIPPED_DRY_RUN`, so no SSH or mutation occurs. With explicit `--apply`, all
three readable operator-input files are required:

```bash
fleetctl deploy --environment develop --source HEAD --apply \
  --bootstrap-vars /protected/bootstrap.yml \
  --compiled-secrets /protected/compiled-secrets.yml \
  --readiness-vars /protected/readiness.yml \
  --cloudflare-token-file /protected/cloudflare-token
```

After readiness the coordinator hands the compiled manifest to the backend and
then reconciles the complete desired Cloudflare record set when the semantic
plan affects DNS. A missing token is a resumable `WAITING_FOR_DNS`, not a failed
or half-promoted deployment. A retry of the same source uses `--resume`, keeps
completed steps and does not resend an accepted manifest. The successful final
status is `RECONCILED`; a plan without DNS impact records DNS as `NOT_REQUIRED`.
The standalone `dns` command remains useful for an operator preview.
On the management executor the token is not a persistent config file: the
environment AppRole reads `kv/<environment>/dns/cloudflare#api_token`, the
resolver writes a temporary mode-`0600` file, and the executor removes it on
exit. The explicit CLI option remains available for a manual deployment.

`update-deployment-ref` is the atomic compare-and-swap that records a deployment
the coordinator has already finished. The coordinator does not call it: moving
the ref means writing to the repository, and the whole point of the split is
that the process holding the fleet's SSH keys never holds that right. The caller
is the `promote` job in `.github/workflows/fleet-deploy.yml`, which runs only
after the deployment record it reads back reports `RECONCILED` for exactly
the environment, source SHA and baseline the run requested. `make fleet-promote`
is the same step by hand.

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
manifests or `nodeagent.v1` requests. They contain secret references, never
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
hardens the host and installs a separate `github-deploy` account. Its keys are
restricted to `platform-readiness` and a strictly parsed `fleet-deploy`
handoff. GitHub stores only its private SSH key and management host; Vault
credentials, fleet SSH keys and resolved values remain on the management VPS.

Bootstrap never initializes or unseals Vault. The operator then uses the
root-owned ceremony command to configure environment-scoped policies/AppRoles
and import values. The local resolver materializes temporary `0600` Ansible
inputs immediately before `--apply`; they are removed on exit. See
`docs/operations/PLATFORM_BOOTSTRAP.md`.

The GitHub workflow accepts only a full commit reachable from `main`, transfers
an exact Git bundle, and invokes the existing coordinator under an environment
lock. The deployment ref moves afterwards, in a separate job with no access to
the bundle, the SSH identity, or the hub — see `update-deployment-ref` above.
