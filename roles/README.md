# Ansible role map

| Role | Responsibility |
|---|---|
| `compiled_node_plan` | Validate and expose one generated node plan |
| `compiled_runtime` | Translate compiled runtime input for component roles |
| `bootstrap_wireguard` | Generate machine-local WireGuard identity and configure management networking |
| `node_layout` | Create protected node directories |
| `pki_agent` | Generate machine-local agent key/CSR and install certificate renewal units |
| `common` | Host baseline and deploy-user/hardening controls |
| `docker` | Docker prerequisites |
| `node_limits` | Persistent CAKE egress ceiling and role-aware fairness |
| `xray` | Compiled Xray runtime configuration |
| `nginx_mask` | REALITY mask service |
| `node_exporter` | Node metrics component |
| `platform_wireguard` | Create the management WireGuard hub and reconcile operator/node public peers without exporting private keys |
| `platform_vault` | Install loopback-only TLS Vault and manual ceremony/policy tooling without automatic init or unseal |
| `platform_executor` | Install the restricted GitHub SSH command gate and local deployment executor |

Role inputs used by v1 come from generated node plans. Component roles must not
read `desired/`, infer topology, or use a hand-maintained fleet inventory.
