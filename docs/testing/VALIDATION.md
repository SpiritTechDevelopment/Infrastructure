# Validation performed before shipping

Validated on 2026-07-14 without connecting to the live fleet.

Passed locally:

- shell syntax for every `scripts/*.sh` file;
- Python bytecode compilation for every `scripts/*.py` file;
- YAML parsing for all repository YAML files;
- Grafana dashboard JSON parsing;
- Ansible syntax checks using `examples/inventory.yml`;
- offline rendering and JSON validation for an entry and exit Xray configuration;
- stateful fake-API tests for Xray user add/list/has/stats/remove behavior;
- production-inventory preflight with temporary matching test certificate/password;
- production rendering for `entry-1`, `exit-fr`, and `exit-ru` with disabled `exit-nl` skipped.

Not run here:

- remote Ansible deployment;
- Docker/Xray `run -test` validation, because Docker/Xray is unavailable in the build container;
- real public Xray gRPC calls;
- real VLESS/REALITY tunnel and exit-IP check;
- live Loki, Prometheus, Grafana, Vault, or provider-network checks.

`make deploy-e2e 2>&1 | tee deploy-e2e.log` performs those live checks and is the
acceptance test for this release.
