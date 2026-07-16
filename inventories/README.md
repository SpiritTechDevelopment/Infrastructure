# Inventory strategy

`inventories/prod/inventory.yml` defines existing SSH access, public customer endpoints,
public Xray API endpoints, TLS material, entry/exit topology, service identities,
control-plane address and enabled-node lifecycle.

Normal deployment does not create users or modify SSH, firewall, fail2ban, WireGuard or
Docker daemon policy. All corresponding guard variables must remain false. The
management-network playbook is a hard stub and legacy private-network fields are not part
of the active contract.

Customer UUIDs do not belong in inventory. The backend adds them through HandlerService
and persists desired state in its database. Only infrastructure-owned entry-to-exit
service identities are static.

Hosts with `node_enabled: false` are excluded from deployment, generated wiring,
verification and observability targets.
