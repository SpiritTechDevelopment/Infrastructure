# Management platform bootstrap

This is a one-time operator procedure. It installs an uninitialized Vault and a
restricted GitHub SSH command gate. It does not deploy fleet nodes.

## 1. Prepare the host

Create one management VPS manually. Independently obtain its public SSH host
key from the provider console or another trusted channel; do not discover trust
with `ssh-keyscan` during deployment.

Edit the two public bootstrap inputs:

- `inventories/bootstrap/platform.yml` — exactly one global IP and `root` user;
- `inventories/bootstrap/known_hosts` — complete public host-key line for that IP.

Copy `examples/platform-bootstrap-vars.yml` outside the repository and fill in
the immutable Vault image digest, reviewed operator/GitHub SSH public keys,
explicit SSH source CIDRs, internal Vault TLS name and stable node ID. No private
key belongs in these files.

Use a different GitHub SSH key pair for each environment. Add each public half
to `platform_github_ssh_keys` with its environment binding; store the matching
private half only in that GitHub Environment. The forced command receives the
binding from root-owned `authorized_keys`, not from workflow input.

## 2. Validate and apply

```bash
make fleet-platform-check
make fleet-platform-bootstrap-check CONNECT=1 \
  PLATFORM_VARS=/protected/platform-bootstrap.yml
make fleet-platform-bootstrap APPLY=1 \
  PLATFORM_VARS=/protected/platform-bootstrap.yml
```

The role hardens SSH/firewall, installs Docker, generates a host-local transport
CA and Vault certificate, starts loopback-only Vault, and installs the
`github-deploy` forced command. Private TLS keys never leave the host.

## 3. Vault ceremony

Connect as an operator and use the Vault CLI inside its container. Initialize it
with an approved key share/threshold policy. Transfer every recovery/unseal key
and the initial root token directly to approved external recovery stores; do not
leave init JSON on the VPS, local workspace, shell history, GitHub or Vault.

Unseal Vault using separate key holders and verify `vault status`. Reboot and
restore tests are required before relying on the platform. Auto-unseal is out of
scope until an independent KMS is selected.

Vault mount/policy configuration, initial secret import, snapshots and the local
deployment identity are not implemented yet. Do not enable mutating GitHub
deployment until those gates and a local `secret://` resolver exist.

## 4. GitHub readiness

For each GitHub Environment set:

- secret `PLATFORM_SSH_PRIVATE_KEY` — private half of the dedicated forced-command key;
- variable `PLATFORM_SSH_HOST` — the IP from the tracked bootstrap inventory.

Run `platform-readiness` manually. GitHub connects with the tracked pinned host
key and can execute only the root-owned readiness command. A sealed or
uninitialized Vault returns a non-zero job result; no secret values are returned.

## Recovery boundary

Loss of the management VPS will require a new bootstrap plus a verified Vault
snapshot restore. Until snapshot automation and a restore drill exist, this
foundation is not production-ready.
