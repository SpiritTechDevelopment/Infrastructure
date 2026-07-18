# Vault SSH certificate authority

Short-lived, overlay-locked SSH certificates signed by Vault, so operators log in
with a **24h cert** instead of a long-lived key in `authorized_keys` (which stay as
break-glass). Data plane unaffected; this is a management-access mechanism.

## What's configured

- **Vault SSH secrets engine** at `ssh-client-signer` (CA mode; the CA signing key
  lives in Vault and never leaves it). CA **public** key is in group_vars
  `common_ssh_ca_public_key`.
- **Roles** (`ssh-client-signer/roles/…`):
  - `operator` — 24h, principals `deploy`/`root`, `permit-pty`, **source-address
    locked to the overlay** (`10.20.0.0/24`), rsa-sha2-256.
  - `automation` — 15m (fresh cert per CI run), `deploy` only, same overlay lock.
- **Host trust** — every host has the CA public key at
  `/etc/ssh/trusted-user-ca-keys.pem` and `TrustedUserCAKeys` in the sshd drop-in
  (`roles/common`), so it accepts CA-signed certs **in addition to** authorized_keys.
- **Overlay lock proven**: a cert works over `wg0` and is `Permission denied` from a
  public source IP.

Codified in `roles/vault` (`vault-ssh-ca.sh` renders the engine/roles/policy) and
`roles/common` (host trust). Re-runnable idempotently.

## Sign a cert and log in

```bash
# 1. get a 24h cert for your key (see "reaching Vault" below for where this runs):
vault write -field=signed_key ssh-client-signer/sign/operator \
  public_key=@~/.ssh/spirit_ops.pub valid_principals=deploy > ~/.ssh/spirit_ops-cert.pub

# 2. ssh — OpenSSH auto-presents <key>-cert.pub next to the key:
ssh -i ~/.ssh/spirit_ops deploy@10.20.0.11    # over the overlay
```

Inspect a cert: `ssh-keygen -L -f ~/.ssh/spirit_ops-cert.pub` (shows the 24h window,
`Principals: deploy`, `Critical Options: source-address 10.20.0.0/24`).

## Operational notes / still to decide

- **Vault is loopback-only** (`vault_bind_address: 127.0.0.1` on control-1), so today
  you sign by SSHing to control-1 (via your authorized_keys) and running `vault write
  …/sign/operator`. To let operators sign **from their own machine**, Vault must be
  reachable over the overlay — the control-1 firewall already permits `8200` from the
  workstation over `wg0`; you'd change `vault_bind_address` to the overlay and
  **redeploy Vault (which restarts → re-seals it → needs unseal again)**. Decide this
  alongside auto-unseal.
- **Auto-unseal not configured** — every Vault/container restart re-seals it (manual
  3-of-5 unseal). A CA that hosts depend on wants auto-unseal (transit/KMS) or an
  accepted manual-unseal runbook.
- **Operator auth** — `vault-ssh-ca.sh` enables `userpass` + writes the `ssh-operator`
  policy (`sign/operator` only). Create a userpass user per operator mapped to that
  policy so they get a scoped token (not the root token) to sign with.
- **Revoke the bootstrap token** — the root token used to configure the CA was placed
  at `/root/.vault-ca-token` on control-1. After setup, **remove it and revoke it**
  (`vault token revoke -self` / rotate the root token). It is not needed for signing.
- **Revocation** — SSH has no CRL; expiry (24h) + the source-address lock are the
  controls. A KRL is break-glass only.

See [OPERATIONS.md](../deploy/OPERATIONS.md) for the access model and
[CONVERGENCE_STATUS.md](../status/CONVERGENCE_STATUS.md) for state.
