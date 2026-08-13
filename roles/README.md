# Ansible role map

## Used by infrastructure v1

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
| `platform_vault` | Install loopback-only TLS Vault and manual ceremony/policy tooling without automatic init or unseal |
| `platform_executor` | Install the restricted GitHub SSH command gate and local deployment executor |

## Retained legacy implementation

`acme`, `alloy`, `backend`, `cloudflare_dns`, `management_wireguard`,
`observability`, `vault`, and `vpn_stack` are retained only because their v1
replacements are not functionally complete. They are not wired into the
compiled `playbooks/deploy/configure.yml` contour and must not be treated as a
second supported deployment path.

Role inputs used by v1 come from generated node plans. Component roles must not
read `desired/`, infer topology, or use the legacy manual production inventory.
