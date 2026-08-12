# Desired-state schemas

JSON Schemas in this directory define the structural contract for
`Environment`, `Platform`, `Fleet`, `LogicalNode`, and `Instance` objects with
`apiVersion: spiritvpn.io/v1alpha1`. Six additional schemas validate the
singleton files under `desired/common/`:

- `components.yml` — image repository, human-readable tag, and immutable digest;
- `networking.yml` — management WireGuard, agent port, and DNS policy;
- `observability.yml` — retention, scrape, and probe intervals;
- `rollout.yml` — fleet concurrency and convergence/drain timeouts;
- `xray.yml` — stable tag conventions and access-log policy;
- `limits.yml` — named VPS bandwidth/CAKE profiles and degradation threshold.

`common-overrides.schema.json` is the strict partial schema used for
`Environment.spec.common_overrides` and `LogicalNode.spec.common_overrides`.
Overrides use deterministic precedence `common < Environment < LogicalNode`;
unknown and derived fields are rejected before the typed deep merge.

`Platform` is optional while an environment is only a placeholder. Once
declared under `desired/environments/<env>/platform/`, it must contain only
non-secret bootstrap topology: the manually provisioned host, independently
verified SSH fingerprints, Vault TLS secret references, and the GitHub Actions
OIDC trust identity. Its presence also requires an immutable Vault image digest.

JSON Schema handles field shape and primitive constraints. Cross-object rules,
such as reference resolution, role matching, environment isolation, and the
single-serving-instance invariant, are implemented in `fleetctl/validation/`.

`null` is intentional only where production input has not been accepted yet. An
empty environment may retain those placeholders, but validation rejects traffic
nodes until their component digests and referenced bandwidth profile are
explicit. Conntrack and Xray file-descriptor limits are observed at runtime and
are not overridden until a load test justifies concrete values.
