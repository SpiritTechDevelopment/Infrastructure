# Documentation

The repository keeps only current v1 documentation. Superseded runbooks and
point-in-time fleet snapshots remain available through Git history.

## Authoritative documents

| Document | Purpose |
|---|---|
| [../contracts/backend/INFRA_DELTA.md](../contracts/backend/INFRA_DELTA.md) | Open decisions and the infrastructure-owned delta to the backend |
| [../contracts/backend/BACKEND_DOMAIN_AGREEMENTS.md](../contracts/backend/BACKEND_DOMAIN_AGREEMENTS.md) | Vendored authority for backend-owned domain behavior |
| [../contracts/nodeagent/v1/node_agent.proto](../contracts/nodeagent/v1/node_agent.proto) | Backend-to-node-agent wire contract |
| [../contracts/desired-state/README.md](../contracts/desired-state/README.md) | Desired-state schema contract |
| [../desired/README.md](../desired/README.md) | Human-edited desired-state layout and commands |
| [../fleetctl/README.md](../fleetctl/README.md) | Compiler, Git baseline, generated Ansible input, and coordinator behavior |

Supporting indexes:

- [architecture/README.md](architecture/README.md)
- [integration/README.md](integration/README.md)
- [operations/README.md](operations/README.md)

Для последовательного знакомства со всей системой без чтения нормативной
спецификации начните с русскоязычного
[операторского гайда](operations/INFRA_V1_GUIDE_RU.md).

Operational procedures that are not implemented by the v1 coordinator are not
documented as available. The current coordinator stops at
`WAITING_FOR_BACKEND`; backend apply, DNS/data-plane promotion, drain/retire,
rollback, and deployment-ref advancement remain future work. The protected
GitHub-to-management handoff is documented under [operations/](operations/).
