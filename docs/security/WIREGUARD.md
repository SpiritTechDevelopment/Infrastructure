# WireGuard status

The WireGuard overlay (`wg0`, `10.20.0.0/24`, hub `control-1` = `10.20.0.1`) is
**live and required for the management plane**. After the overlay-first hardening,
the Xray API (`10085`), telemetry ingest (`9090`/`3100`), and operator access to
Grafana/Vault are reachable **only** over it — you must be a `wg0` peer to operate
the fleet. See [ARCHITECTURE.md](../architecture/ARCHITECTURE.md) §7 and [OPERATIONS.md](../deploy/OPERATIONS.md).

**Codified — but not yet re-applied to the live overlay.** `roles/management_wireguard`
is un-stubbed and runnable:

```bash
make management     # runs playbooks/management-network.yml over the whole
                    # management_network group (control-1/entry-1/exit-fr), no --limit
```

The inputs live in `inventories/prod/group_vars/all.yml`
(`management_wireguard_addresses`, `_public_endpoint`, `_external_peers` =
exit-ru + the operator workstation) and the `management_network` inventory group.
The role reuses each host's existing on-node key (never regenerates), and an
offline render was proven **functionally identical** to the live `wg0.conf` on
every host (same addresses/keys/peers/AllowedIPs/routing).

> **The live overlay is still the hand-configured version.** The first
> `make management` run rewrites the on-disk configs to the role's canonical form
> (cosmetic header/comment/whitespace changes — exit-fr's hand-onboard header, the
> `# exit-ru` comment) and therefore **restarts `wg-quick@wg0`**, a brief
> overlay/telemetry blip (the data plane is unaffected). Apply it in a maintenance
> window with **provider console ready** — restarting the hub's `wg0` bounces every
> peer, and you are typically tunnelled through the overlay you're restarting.
> Managing `wg0` from a machine on that same overlay will drop your own connection
> mid-run; run it over the hosts' public SSH, or from the console.

Do not copy commands from `docs/legacy/` into production.
