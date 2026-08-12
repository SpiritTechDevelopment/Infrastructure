# Backend manifest contract

This directory contains the pinned protobuf contract consumed by the
infrastructure deployment pipeline.

## Provenance

```text
repository  git@github.com:SpiritTechDevelopment/SpiritVPN.git
file        proto/spiritvpn/manifest/v1/manifest.proto
commit      91326dad33678e30344904c75e7cff17621bc455
date        2026-08-09
sha256      bbbe8b19780187eac043eb124609df112e6c863d9009dd2de0036bc328b67ce9
```

The vendored file is byte-for-byte identical to the source at that commit.
Changes must originate in the backend repository and be reviewed against the
compiler and deployment state machine before this copy is updated.

The v1 service exposes only `ApplyFleetManifest`. `APPLIED` and `IDEMPOTENT`
are successful deployment-boundary results. Materialization and delivery of
agent operations are asynchronous backend responsibilities observed through
metrics and alerts; this contract provides no validation or convergence-status
RPC.
