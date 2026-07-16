# V4 changes

- added a first-class hub-and-spoke WireGuard management-network role
- added node-generated persistent WireGuard keypairs and root-only wg-quick configs
- added external controller/backend peer generation tooling
- added a management-network playbook and mandatory staged deployment order
- integrated hub UDP/51820 and wg0-to-wg0 forwarding into nftables
- bound Xray API firewall access to wg0 and backend/controller CIDRs
- split platform ingestion CIDRs from Vault/Grafana/Alertmanager administrator CIDRs
- changed the sample to one 10.20.0.0/24 management network
- added WireGuard bootstrap, server-add and troubleshooting documentation
- preserved public Xray TCP/443 and localhost nginx decoy behavior independently of WireGuard
