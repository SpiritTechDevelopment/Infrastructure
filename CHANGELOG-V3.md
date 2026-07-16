# V3 changes

- removed the speculative node-agent role and fleet-user playbook
- removed the speculative backend/panel dynamic-inventory plugin
- enabled Xray HandlerService and StatsService on private per-node management addresses
- added nftables allowlisting for the Xray API port
- switched to Xray's simplified gRPC API listener configuration
- separated infrastructure-only `xray_static_clients` from runtime customer users
- added direct API add/list/remove/stats smoke tooling
- documented runtime state replay after Xray restart
- renamed Vault's application identity and policy from `panel` to `backend`
- added Vault AppRole verification instructions
- updated CI to require an explicit production limit
- pinned Ansible collection versions
- fixed offline Xray rendering and a Prometheus/Jinja escaping defect
