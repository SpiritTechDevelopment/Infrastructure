# Desired state

This directory is the human-edited source of truth for infrastructure topology.
It must contain references to secrets, never secret values.

- `common/` contains shared defaults encrypted in place with SOPS.
- `fleet-ids.yml` is the SOPS-encrypted append-only mapping from fleet
  identifiers to numeric `vpn_fleet_id` values.
- `environments/` contains one encrypted topology for each isolated
  `develop` and `prod` environment.

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

`develop` declares the live fleet: one entry in Russia and one exit in Romania.
`prod` is still an intentionally empty placeholder, containing
neither a fleet nor a control release. A complete synthetic fleet and control
stack live under `tests/fixtures/valid/`.

Each live environment is represented by one `EnvironmentTopology` document
named `topology.sops.yml`. Per-object files remain supported only for synthetic
test fixtures; they may not be mixed with a topology bundle. Git carries Vault
references, never secret values, and encrypts the complete environment payload,
including addresses, ports, domains and release pins.

The management executor and the dedicated self-hosted runner each own a separate
age identity with mode `0600`. Their private identities are neither returned to
GitHub nor committed to Git; trusted processes receive only the local file path
through `SOPS_AGE_KEY_FILE`. A third recipient is held for operator recovery.

`fleetctl` decrypts a SOPS document to process memory and never writes plaintext
beside it. A missing or invalid age identity is a validation failure, not a
reason to accept the encrypted placeholders as desired state.

Some components are declared through a reviewed registry mirror because direct
access to their canonical registries is unavailable from one traffic-node
network. See the comments in `common/components.yml`.
