# Backend integration contract

## Endpoint discovery

A successful deployment writes:

```text
generated/client-endpoints.json
```

For each active entry it contains the public customer address/port, REALITY server name,
short ID, derived client password, fingerprint, default exit tag, Xray image, and public API
endpoint. The file is mode `0600` and must be treated as sensitive client bootstrap data.

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

## Routing

Ordinary API-created users are routed through `entry_default_exit_tag`. Infrastructure-only
selector identities named `via-<country>` are reserved for operator smoke tests and must not
be used as customer IDs.
