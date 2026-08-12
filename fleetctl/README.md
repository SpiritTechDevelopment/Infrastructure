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
         → render/Ansible input validation → bootstrap → configure → readiness
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

Node plans are infrastructure topology projections, not backend manifests or
`infraagent.v1` requests. They contain secret references, never resolved secret
values.
