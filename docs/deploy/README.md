# Deploy & operate

How to run, change, and grow the fleet. Deploys run from an operator workstation on
the WireGuard overlay; commits do **not** auto-deploy (CI only lints).

| Doc | What |
|---|---|
| [GETTING_STARTED.md](GETTING_STARTED.md) | **Cloned it and want to run + monitor it? Start here** — prerequisites → setup → configure → deploy → monitor |
| [OPERATIONS.md](OPERATIONS.md) | Access model, Git workflow, secrets/`make decrypt`, onboarding an operator, monitoring, Vault unseal (§9) |
| [what-deploys-what.md](what-deploys-what.md) | The command → playbook → hosts → roles map, and the blast radius of each |
| [ONBOARDING_AND_HARDENING.md](ONBOARDING_AND_HARDENING.md) | Add / replace a server: bootstrap, hardening, overlay join, wiring |
| [FIRST_RUN.md](FIRST_RUN.md) | First-time deployment walkthrough |
| [CUTOVER.md](CUTOVER.md) | Move CI from a hosted runner to a self-hosted one on the overlay |
| [RECOVERY.md](RECOVERY.md) | Disaster recovery — surviving a lost laptop / key |
| [PRODUCTION.md](PRODUCTION.md) | Current operational boundary |
| [SETUP-acme.md](SETUP-acme.md) | ACME (certbot + Cloudflare) certificate setup |

**Golden rules:** deploy node-scoped (`apply-node LIMIT=`) to bound impact; reconcile
runtime users after any Xray restart; firewall/access changes go through
`harden.yml`/`access.yml` with the dead-man discipline, never a routine deploy. See
[what-deploys-what.md](what-deploys-what.md).
