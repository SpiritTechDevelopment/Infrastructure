# Fleet quick reference

```bash
make deploy-e2e                       # full deployment and customer-path proof
make verify                           # runtime/telemetry verification only
make e2e ENTRY=entry-1                # API/customer path only
make apply-node LIMIT=exit-fr         # selected-node redeploy
make wire                             # rebuild entry outbounds from active exits
```

Backend operations:

```bash
make api-ping NODE=entry-1
make api-add NODE=entry-1 UUID="$UUID" EMAIL="$EMAIL"
make gen-client NODE=entry-1 UUID="$UUID" EMAIL="$EMAIL" OUT=client.json
make api-remove NODE=entry-1 EMAIL="$EMAIL"
```

No target in this repository configures SSH, firewall, Fail2ban or WireGuard.
