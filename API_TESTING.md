# Xray API test workflow

The active Xray API is native gRPC on TCP/10085, now **overlay-only** — reachable
only over the WireGuard overlay (`xray_api_overlay_host`, e.g. `10.20.0.11`), not
publicly. `make api-*` targets the overlay host automatically; you must be a `wg0`
peer. The wrapper accepts either an inventory host name or an explicit `host:port`.

```bash
make api-ping NODE=entry-1
make api-list NODE=entry-1
```

Add and verify a unique user:

```bash
UUID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
EMAIL="api-test-$(date +%s)@example.invalid"
make api-add NODE=entry-1 UUID="$UUID" EMAIL="$EMAIL"
make api-has NODE=entry-1 EMAIL="$EMAIL"
```

Generate a profile and test it manually:

```bash
make gen-client NODE=entry-1 UUID="$UUID" EMAIL="$EMAIL" OUT=client-api-test.json
xray run -c client-api-test.json
curl --socks5-hostname 127.0.0.1:10808 https://api.ipify.org
```

Inspect counters and remove the user:

```bash
make api-stats NODE=entry-1 PATTERN="$EMAIL"
make api-remove NODE=entry-1 EMAIL="$EMAIL"
```

For the complete automated proof, including expected exit IP and post-removal rejection:

```bash
make e2e ENTRY=entry-1
```

The API has no built-in authentication — the WireGuard overlay is its access
control. Port 10085 is **not** public (firewalled to `10.20.0.0/24` over `wg0`);
keep it that way.
