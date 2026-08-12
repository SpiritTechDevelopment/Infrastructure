# Documentation

The repository keeps only current v1 documentation. Superseded runbooks,
point-in-time fleet snapshots, duplicate infrastructure specifications, and the
direct-Xray/manual-inventory deployment guides were removed; Git history remains
the archive.

## Authoritative documents

| Document | Purpose |
|---|---|
| [architecture/INFRA_TECHNICAL_SPEC.md](architecture/INFRA_TECHNICAL_SPEC.md) | Approved normative infrastructure v1 specification |
| [status/INFRA_V1_IMPLEMENTATION_STATUS.md](status/INFRA_V1_IMPLEMENTATION_STATUS.md) | Implemented capabilities, limitations, and ordered next work |
| [../contracts/backend/BACKEND_DOMAIN_AGREEMENTS.md](../contracts/backend/BACKEND_DOMAIN_AGREEMENTS.md) | Vendored authority for backend-owned domain behavior |
| [../contracts/nodeagent/v1/node_agent.proto](../contracts/nodeagent/v1/node_agent.proto) | Backend-to-node-agent wire contract |
| [../contracts/desired-state/README.md](../contracts/desired-state/README.md) | Desired-state schema contract |
| [../desired/README.md](../desired/README.md) | Human-edited desired-state layout and commands |
| [../fleetctl/README.md](../fleetctl/README.md) | Compiler, Git baseline, generated Ansible input, and coordinator behavior |

Supporting indexes:

- [architecture/README.md](architecture/README.md)
- [integration/README.md](integration/README.md)
- [status/README.md](status/README.md)

Operational procedures that are not implemented by the v1 coordinator are not
documented as available. The current coordinator stops at
`WAITING_FOR_BACKEND`; backend apply, DNS/data-plane promotion, drain/retire,
rollback, protected runner operation, and deployment-ref advancement remain
future work.
