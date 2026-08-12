# Transitional GitHub-hosted orchestration

Status: temporary implementation boundary for infrastructure v1.

GitHub Actions is the user-facing orchestrator. The management VPS is the
trusted executor: Vault and all resolved secrets remain there, and GitHub never
connects to Vault or fleet nodes directly.

```text
GitHub Actions -- restricted SSH command --> management VPS
                                            ├── Vault on 127.0.0.1
                                            ├── fleetctl and Ansible (future)
                                            └── management-network access
```

## Trust boundary

- The only GitHub secret is an environment-scoped SSH private key.
- Each matching key is installed on the separate `github-deploy` account with an
  OpenSSH forced command, an immutable environment argument and `restrict`
  options. A workflow cannot claim another environment through command input.
- The root-owned command gate rejects every operation except an explicit
  allowlist. It never evaluates arbitrary arguments or a shell command supplied
  by GitHub.
- The management host and complete public `known_hosts` line are configured
  separately; `StrictHostKeyChecking=yes` is mandatory and `ssh-keyscan` is not
  used in CI.
- GitHub Environment approval and a per-environment concurrency group wrap each
  workflow.
- Vault binds only to loopback. Static Vault tokens and resolved secrets in
  GitHub are forbidden.

GitHub-hosted source addresses change over time. Key-only SSH may therefore
need broader ingress than a private runner would require. The host remains
protected by the forced command, pinned host key, fail2ban and a dedicated key;
moving SSH behind a stable private runner remains the exit condition.

## Current authorization

`.github/workflows/platform-readiness.yml` invokes only
`platform-readiness`. The management host locally checks Vault status and
returns non-secret JSON. GitHub cannot initialize/unseal Vault, read secrets,
run Ansible, call provider/DNS/backend APIs, or move `refs/deployments/*`.

The next handoff increment will add a second fixed command that accepts a strict
environment and full commit SHA, materializes that exact commit in an isolated
directory, resolves secrets locally, and invokes the existing coordinator.
It will be enabled only after Vault policies and the local resolver exist.
