# Contracts

Versioned machine-readable contracts shared with services outside this infrastructure
repository.

| Contract | Purpose |
|---|---|
| [`manifest/v1/manifest.proto`](manifest/v1/manifest.proto) | Infrastructure-to-backend full fleet manifest apply contract |
| [`nodeagent/v1/node_agent.proto`](nodeagent/v1/node_agent.proto) | Backend-to-entry-agent control, inventory, health, usage, and privacy-reduced activity delivery |

Compatibility rules:

- a published package version is immutable;
- additive protobuf changes use new field numbers;
- removed field numbers are reserved;
- breaking changes require a new package (`v2`, and so on);
- generated code belongs in the consuming backend/agent repositories, not here.

The normative backend behavior is vendored in
[`backend/BACKEND_DOMAIN_AGREEMENTS.md`](backend/BACKEND_DOMAIN_AGREEMENTS.md).
Infrastructure-owned requirements are in
[`docs/architecture/INFRA_TECHNICAL_SPEC.md`](../docs/architecture/INFRA_TECHNICAL_SPEC.md).
