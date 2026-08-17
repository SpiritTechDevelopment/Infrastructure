# Desired state

This directory is the human-edited source of truth for infrastructure topology.
It must contain references to secrets, never secret values.

- `common/` contains shared non-secret defaults.
- `fleet-ids.yml` is the append-only mapping from fleet identifiers to numeric
  `vpn_fleet_id` values.
- `environments/` contains isolated `develop` and `prod` objects.

An `Environment` may also contain `spec.control`: immutable backend,
migration and PostgreSQL images plus environment-scoped Vault references. The
compiler then emits `control-plan.json`. The complete non-secret example is in
`tests/fixtures/valid/desired/environments/develop/environment.yml`.

Validate all environments without network access:

```bash
make fleet-validate
```

Render the current environment projections:

```bash
make fleet-render ENVIRONMENT=develop
```

Compare it with an explicit previously deployed desired-state directory:

```bash
make fleet-plan ENVIRONMENT=develop BASELINE=path/to/desired
```

The checked-in environment objects are intentionally empty placeholders: they
contain neither a real fleet nor a control release. A complete synthetic fleet
and control stack live under `tests/fixtures/valid/`.
