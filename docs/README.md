# Documentation

Start at the root [README.md](../README.md) for the mental model and quickstart.
This folder holds the full docs, grouped by purpose. Each subfolder has its own
guide (README) describing what's inside.

| Folder | Guide | What's there |
|---|---|---|
| [architecture/](architecture/) | [guide](architecture/README.md) | How the system is built: layered view, three planes, container networking, and the config-driven topology model |
| [deploy/](deploy/) | [guide](deploy/README.md) | How to operate & deploy: the ops/access model, **what deploys what**, onboarding, recovery, cutover, cert setup |
| [integration/](integration/) | [guide](integration/README.md) | Backend contract: runtime-user lifecycle, quota snapshot, API usage |
| [security/](security/) | [guide](security/README.md) | Security posture: **WireGuard overlay** setup, Vault SSH CA, exposure model |
| [reference/](reference/) | [guide](reference/README.md) | Design-intent specs + deploy-mode reference |
| [status/](status/) | [guide](status/README.md) | Living state: current snapshot, next steps, open decisions, convergence log |
| [testing/](testing/) | [guide](testing/README.md) | Test & validation procedures |
| [archive/](archive/) | [guide](archive/README.md) | Superseded/point-in-time notes, kept for history only |

## Common entry points

- **New here?** [architecture/ARCHITECTURE.md](architecture/ARCHITECTURE.md), then
  [deploy/OPERATIONS.md](deploy/OPERATIONS.md).
- **Deploying / changing the fleet?** [deploy/what-deploys-what.md](deploy/what-deploys-what.md)
  and [architecture/TOPOLOGY.md](architecture/TOPOLOGY.md) (blast radius of a change).
- **Adding/replacing a server?** [deploy/ONBOARDING_AND_HARDENING.md](deploy/ONBOARDING_AND_HARDENING.md).
- **Building the backend?** [integration/BACKEND_INTEGRATION.md](integration/BACKEND_INTEGRATION.md).
- **Security / overlay?** [security/README.md](security/README.md).
- **Where does the project stand?** [status/CURRENT_STATE.md](status/CURRENT_STATE.md).
