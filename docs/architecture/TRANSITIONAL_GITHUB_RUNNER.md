# Transitional GitHub-hosted orchestration

Status: temporary implementation boundary for infrastructure v1.

GitHub Actions is the user-facing orchestrator. The management VPS is the
trusted executor: Vault and all resolved secrets remain there, and GitHub never
connects to Vault or fleet nodes directly.

```text
GitHub Actions -- restricted SSH command --> management VPS
                                            ├── Vault on 127.0.0.1
                                            ├── fleetctl and Ansible
                                            └── management-network access
```

## Trust boundary

- The only GitHub secret is an environment-scoped SSH private key. Vault
  AppRole credentials and the fleet SSH key are root-owned files on the
  management host.
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

`.github/workflows/platform-readiness.yml` invokes only `platform-readiness`.
The management host checks Vault status and returns non-secret JSON.

`.github/workflows/fleet-deploy.yml` accepts an environment, a full commit SHA
reachable from `main`, `dry-run|apply`, and three boolean guards. It sends an
exact Git bundle over the same restricted SSH channel. The root-owned executor:

1. rejects unknown refs and a SHA mismatch;
2. imports the source and optional deployment baseline into a local bare repo;
3. checks out the exact commit in an isolated directory;
4. for `apply`, resolves only that environment's `secret://` references from
   loopback Vault and materializes temporary `0600` Ansible inputs;
5. invokes the existing resume-safe coordinator with a persistent,
   environment-locked state directory;
6. destroys temporary resolved secrets and the worktree on exit.

The workflow cannot initialize/unseal/configure Vault, return secret values,
call provider or DNS APIs, apply the backend manifest, or move
`refs/deployments/*`. Until the backend adapter exists the final coordinator
state remains `WAITING_FOR_BACKEND` even after successful infrastructure apply.

`.github/workflows/control-deploy.yml` sends one reviewed infrastructure commit
to an environment-bound root executor. The executor renders `control-plan.json`,
resolves only `control` Vault references and reconciles the local PostgreSQL,
migrations and backend stack. GitHub receives neither Vault credentials nor
resolved application secrets. The local successful control release ref is not
the fleet deployment baseline and does not bypass `ApplyFleetManifest`.

GitHub transfers source objects rather than giving the management VPS a GitHub
token or repository deploy key. Environment protection and review of the exact
SHA remain part of the authorization boundary.
