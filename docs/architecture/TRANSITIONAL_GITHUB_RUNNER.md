# Transitional GitHub-hosted runner

Status: temporary implementation boundary for infrastructure v1.

The target architecture still requires a dedicated protected management
runner. Until it exists, GitHub-hosted Actions may perform explicitly scoped
operations after the operator has bootstrapped the management host.

## Trust and transport

- Vault API remains bound to `127.0.0.1`; it is not exposed to the Internet.
- Actions reaches Vault only through an SSH tunnel to the management host.
- The SSH private key and complete `known_hosts` entry are GitHub Environment
  secrets. The public fingerprint is committed in the `Platform` descriptor.
- `fleetctl platform-known-hosts-check` must match the supplied public host key
  to a reviewed fingerprint before any SSH connection.
- GitHub Environment approval and a per-environment concurrency group are
  required for every remote workflow.
- GitHub OIDC will authenticate to Vault with a short-lived token after the
  handoff implementation exists. Static Vault tokens are forbidden in GitHub.

GitHub-hosted source addresses are not a stable management-network identity.
During this transitional period the management host must expose key-only SSH
to explicitly approved source CIDRs. Broad SSH exposure, if temporarily chosen
to accommodate hosted-runner address churn, is recorded security debt and must
remain protected by pinned host keys, fail2ban and environment-scoped keys.

## Current authorization

`.github/workflows/platform-readiness.yml` is read-only. It renders and checks
the committed platform plan, validates the pinned host key, connects as the
named `deploy` user and verifies that Vault is initialized and unsealed. It has
no `id-token: write` permission and cannot configure Vault or resolve secrets.

No GitHub workflow currently initializes/unseals Vault, stores recovery keys,
writes production secrets, deploys fleet nodes, calls provider/DNS/backend APIs,
or moves `refs/deployments/*`.

## Exit condition

This transitional mode ends when a dedicated management runner can authenticate
to Vault, resolve `secret://` references, reach the private management network,
and execute the existing coordinator with the same environment lock and
approval semantics. The GitHub-hosted remote mutation path must then be removed.
