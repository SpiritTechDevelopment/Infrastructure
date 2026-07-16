# Local secrets and state examples

**Deploy secrets no longer live here.** The Grafana password and the TLS
certificate/key are now SOPS-encrypted in `inventories/prod/secrets.sops.yml`
(committed) and materialized with `make decrypt` → `secrets.plain.yml` (gitignored),
which the deploy passes as extra-vars. See `OPERATIONS.md` §3. To add/rotate a
deploy secret: `sops inventories/prod/secrets.sops.yml`.

`.local-secrets/` is only for **out-of-band break-glass material that must never
enter Git or SOPS** — e.g. `vault-init.json` (Vault unseal keys + root token) and
per-node `wireguard/` private keys. Ideally move even those to a password manager /
offline store. The directory is gitignored and must never be committed or included
in support archives.

`acme.yml.example` is for the separate certificate-issuance play. The desired-users JSON
is an example backend reconciliation state file, not an infrastructure secret store.

Rotate any credentials that appeared in earlier repository copies.
