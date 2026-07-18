# Backend integration contract

## Endpoint discovery

A successful deployment writes:

```text
generated/client-endpoints.json
```

For each active entry it contains the public customer address/port, REALITY server name,
short ID, derived client password, fingerprint, default exit tag, Xray image, and the Xray
API endpoint (the **overlay** address, `xray_api_overlay_host` — the API is overlay-only).
The file is mode `0600` and must be treated as sensitive client bootstrap data.

## Runtime user lifecycle

The backend owns customer UUIDs and accounting identifiers. Infrastructure owns only the
entry-to-exit service users.

Equivalent repository operations are:

```bash
make api-add NODE=entry-1 UUID="$UUID" EMAIL="$ACCOUNTING_ID"
make api-has NODE=entry-1 EMAIL="$ACCOUNTING_ID"
make api-stats NODE=entry-1 PATTERN="$ACCOUNTING_ID"
make api-remove NODE=entry-1 EMAIL="$ACCOUNTING_ID"
```

`EMAIL` is Xray's accounting identifier; it does not have to be deliverable email, but this
repository restricts it to 1-128 safe ASCII characters: letters, digits, `. _ @ : + -`.
Use a globally unique stable identifier.

## Client profile

```bash
make gen-client \
  NODE=entry-1 \
  UUID="$UUID" \
  EMAIL="$ACCOUNTING_ID" \
  OUT=client.json
```

The generated profile uses VLESS, `xtls-rprx-vision`, TCP, REALITY, the entry's public
hostname, and the deployed REALITY client password.

## Persistence and reconciliation

HandlerService changes are in-memory runtime state. A process/container restart discards
API-created users. The backend database is authoritative and must replay desired users after
restart or deployment.

State file example:

```json
{
  "users": [
    {
      "uuid": "11111111-1111-4111-8111-111111111111",
      "email": "device-001",
      "flow": "xtls-rprx-vision"
    }
  ]
}
```

Reconcile without deleting unknown users:

```bash
make reconcile NODE=entry-1 STATE=/secure/path/desired-users.json
```

Authoritative reconciliation:

```bash
make reconcile NODE=entry-1 STATE=/secure/path/desired-users.json PRUNE=1
```

Use `REPLACE=1` only when the UUID behind an existing accounting identifier must be replaced.

### Node-local auto-reconcile snapshot (self-heal on restart)

Each entry runs a systemd timer (`spirit-xray-reconcile.timer`, ~30 s) that re-adds the
backend's desired users after an Xray restart — so an *unplanned* restart self-heals in
seconds instead of stranding customers until the backend replays. It reads a per-entry
**snapshot the backend must write** at `/var/lib/xray/desired-users.json` (same
`{"users":[…]}` format as above).

Contract for the backend:

- **Write it atomically** — render to a temp file and `rename()` into place; mode `0600`.
  A half-written file would be read mid-update.
- **It is a per-entry export of desired state** — write the snapshot for entry *N* with the
  users that belong on entry *N*.
- **The node timer is ADD-ONLY** — it never prunes. A stale snapshot can therefore only
  *add* users, never remove a live one. All removals stay backend-driven (`make reconcile …
  PRUNE=1`, or a direct `remove`).
- **Update the snapshot BEFORE enforcing a removal.** When you suspend a user (quota breach,
  offboarding), drop them from the snapshot *first*, then remove them from Xray — otherwise
  the add-only timer re-adds the just-removed user within one interval.
- **Absent or empty snapshot → the timer no-ops** (safe on a fresh node before the backend
  has written one).

This snapshot does not replace the authoritative backend DB or `make reconcile`; it is a
restart-recovery cache the node can act on without the backend being reachable.

## Routing

Ordinary API-created users are routed through `entry_default_exit_tag`. Infrastructure-only
selector identities named `via-<country>` are reserved for operator smoke tests and must not
be used as customer IDs.
