# Captured live state (Phase 0)

Faithful, **sanitized** snapshots of the live fleet's host-level state at the
start of the hardening convergence (see `../ONBOARDING_AND_HARDENING.md` §6).

Purpose: the ground truth that the repo must be able to reproduce with **zero
diff** (Phase 1) before any managed change is applied (Phase 3+).

**Sanitized:** WireGuard `PrivateKey` lines are redacted. No private key
material, no secrets. Firewall rules and public keys only.

Not consumed by any playbook — reference material for building the templates.
