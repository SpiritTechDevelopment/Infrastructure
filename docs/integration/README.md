# Integration contracts

Integration behavior is defined by the owning contracts rather than a second
descriptive compatibility document:

| Contract | Authority |
|---|---|
| [Backend domain agreement](../../contracts/backend/BACKEND_DOMAIN_AGREEMENTS.md) | Backend-owned domain model, manifest semantics, access, accounting, and agent operations |
| [node_agent.proto](../../contracts/nodeagent/v1/node_agent.proto) | Backend-to-node-agent wire API |
| [Infrastructure specification](../architecture/INFRA_TECHNICAL_SPEC.md) | Infrastructure-owned topology, rollout, PKI, readiness, and required backend delta |
| [Desired-state schemas](../../contracts/desired-state/README.md) | Structural input contract consumed by `fleetctl` |

The old direct-Xray API and add-only snapshot documentation was removed. It
described the isolated legacy runtime and is not a supported v1 integration
surface.
