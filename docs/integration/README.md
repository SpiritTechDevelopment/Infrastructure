# Integration contracts

Integration behavior is defined by the owning contracts rather than a second
descriptive compatibility document:

| Contract | Authority |
|---|---|
| [Backend domain agreement](../../contracts/backend/BACKEND_DOMAIN_AGREEMENTS.md) | Backend-owned domain model, manifest semantics, access, accounting, and agent operations |
| [node_agent.proto](../../contracts/nodeagent/v1/node_agent.proto) | Backend-to-node-agent wire API |
| [Desired-state schemas](../../contracts/desired-state/README.md) | Structural input contract consumed by `fleetctl` |

Direct Xray mutation is not an integration surface. Runtime customer ownership
belongs to the backend/node-agent contract.
