# Architecture

[`INFRA_TECHNICAL_SPEC.md`](INFRA_TECHNICAL_SPEC.md) is the only normative
infrastructure architecture document.

It defines the environment/fleet/logical-node/instance model, source-of-truth
boundaries, compiler invariants, generated artifacts, PKI, rollout ordering,
readiness gates, backend and agent boundaries, DNS, observability, failure
behavior, and acceptance phases.

Earlier architecture guides, topology notes, and the proposed repository
blueprint were removed because they described the manual production inventory,
direct Xray control, or a replicated-node model rejected by the approved v1
specification.
