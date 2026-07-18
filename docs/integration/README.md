# Integration

The contract between this infrastructure and the **backend** that manages customers.

| Doc | What |
|---|---|
| [BACKEND_INTEGRATION.md](BACKEND_INTEGRATION.md) | The runtime-user lifecycle, the desired-state reconcile contract, the node-local auto-reconcile snapshot, and (design) the quota accounting model |
| [API_TESTING.md](API_TESTING.md) | Exercising the Xray gRPC API (add/remove/list/stats) |

**Key facts the backend must honor:**
- Runtime users are **in-memory** at the entry; the backend owns the authoritative
  list and replays it (`make reconcile` / the on-node snapshot).
- User identity + quota accounting are **entry-scoped** — exit topology is invisible
  to the backend (see [../architecture/TOPOLOGY.md](../architecture/TOPOLOGY.md)).
- The API is **overlay-only** (`:10085`) — the backend must be a `wg0` peer.
