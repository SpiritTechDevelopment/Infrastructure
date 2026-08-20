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

`develop` declares the live fleet: one entry in Russia and one exit in Romania.
`prod` is still an intentionally empty placeholder, containing
neither a fleet nor a control release. A complete synthetic fleet and control
stack live under `tests/fixtures/valid/`.

An environment may be represented either by the current per-object files or by
one `EnvironmentTopology` document named `topology.yml` (eventually
`topology.sops.yml`). The two layouts compile to identical runtime artifacts and
may not be mixed. Migration of the live environments waits for the management
executor to receive a reviewed SOPS decryption identity; Git continues to carry
only Vault references, never secret values.

The management executor creates that age identity locally with mode `0600` and
publishes only its recipient into a separate root-readable file. The private
identity is neither returned to CI nor committed to Git; deployment processes
receive only its file path through `SOPS_AGE_KEY_FILE`.

`fleetctl` decrypts a SOPS document to process memory and never writes plaintext
beside it. A missing or invalid age identity is a validation failure, not a
reason to accept the encrypted placeholders as desired state.

Three components are declared through `mirror.gcr.io` rather than their
canonical registries. Docker Hub and quay.io are unreachable from the network
`entry-1` sits in, so a canonical declaration would leave the entry unable to
pull three of its four components. See the comment in `common/components.yml`.
