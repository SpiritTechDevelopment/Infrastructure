# fleetctl

`fleetctl` is the offline compiler for SpiritVPN infrastructure desired state.
The model, validation, and compiler packages perform no network I/O. Filesystem
writes are isolated in `fleetctl/adapters/`.

Implemented commands:

```bash
fleetctl validate --environment develop
fleetctl render --environment develop --output build/develop
fleetctl plan --environment develop --baseline path/to/desired --output build/develop
```

The current render increment produces:

```text
build/<environment>/
├── ansible-inventory.json
├── dns-plan.json
├── monitoring-targets.json
├── node-plans/<instance_id>.json
└── impact-plan.json                  # after fleetctl plan
```

The initial planner takes an explicit baseline `desired/` directory. Resolving
`refs/deployments/<environment>` belongs to a later Git adapter.

Node plans are infrastructure topology projections, not backend manifests or
`infraagent.v1` requests. They contain secret references but never resolved
secret values.
