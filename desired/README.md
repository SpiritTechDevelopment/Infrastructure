# Desired state

This directory is the human-edited source of truth for infrastructure topology.
It must contain references to secrets, never secret values.

- `common/` contains shared non-secret defaults.
- `fleet-ids.yml` is the append-only mapping from fleet identifiers to numeric
  `vpn_fleet_id` values.
- `environments/` contains isolated `develop`, `staging`, and `prod` objects.

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

The checked-in environment objects are intentionally empty placeholders. A
complete synthetic fleet lives under `tests/fixtures/valid/`.
