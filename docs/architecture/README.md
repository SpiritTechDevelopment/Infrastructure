# Architecture

[`INFRA_TECHNICAL_SPEC.md`](INFRA_TECHNICAL_SPEC.md) is the only normative
infrastructure architecture document.

[`TRANSITIONAL_GITHUB_RUNNER.md`](TRANSITIONAL_GITHUB_RUNNER.md) records the
temporary GitHub-hosted runner boundary without changing the protected-runner
target in the normative specification.

It defines the environment/fleet/logical-node/instance model, source-of-truth
boundaries, compiler invariants, generated artifacts, PKI, rollout ordering,
readiness gates, backend and agent boundaries, DNS, observability, failure
behavior, and acceptance phases.

Superseded architecture drafts remain available in Git history. This directory
contains only the approved v1 model.
