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

The bootstrap installs `/usr/local/sbin/spiritvpn-vault-operator`. It is the
only supported manual interface for the initial ceremony; run it from the
operator account through `sudo`. It never stores an unseal share or root token.

First confirm that the new Vault is reachable and uninitialized:

```bash
ssh -t deploy@MANAGEMENT_HOST \
  sudo /usr/local/sbin/spiritvpn-vault-operator status
```

Initialize with the approved share policy. The command below is an example, not
a universal recovery policy:

```bash
ssh deploy@MANAGEMENT_HOST \
  sudo /usr/local/sbin/spiritvpn-vault-operator init 5 3
```

The JSON response is emitted once. Direct it immediately into approved external
recovery storage. Do not save it on the VPS, in this repository, under
`.local-secrets`, in shell history, or in GitHub. Distribute the shares to
separate holders and store the initial root token separately.

Three different holders then unseal interactively:

```bash
ssh -t deploy@MANAGEMENT_HOST \
  sudo /usr/local/sbin/spiritvpn-vault-operator unseal
```

Repeat after every Vault restart. Auto-unseal remains deliberately absent until
an independent KMS exists.

## 4. Configure policies and import initial secrets

For every environment that will be used, create its KV policy and a loopback-
bound AppRole for the local executor:

```bash
ssh -t deploy@MANAGEMENT_HOST \
  sudo /usr/local/sbin/spiritvpn-vault-operator configure develop
```

The command prompts for the temporary initial root token and writes only the
environment-scoped AppRole ID/secret ID to
`/etc/spiritvpn/deploy/develop/vault-approle/`. Re-running it rotates the local
secret ID and revokes the previously recorded accessor. GitHub receives none of
these values. After configuring all required environments, return the initial
root token to offline recovery storage.

List the exact references required by a reviewed source checkout:

```bash
python3 scripts/vault-secret-resolver.py \
  --root . --environment develop --list-references
```

Import each value interactively. The final reference in the list is the private
SSH identity used by Ansible; its public half must be authorized on the fleet
hosts. The value is read from standard input and is not placed in the command
line:

```bash
ssh -t deploy@MANAGEMENT_HOST \
  sudo /usr/local/sbin/spiritvpn-vault-operator put develop \
  secret://kv/develop/executor/ansible#private_key
```

Repeat for every desired-state reference. `put` refuses cross-environment paths
and empty values.

## 5. Install non-secret executor inputs

Create these root-owned files on the management host:

```text
/etc/spiritvpn/deploy/develop/bootstrap.yml
/etc/spiritvpn/deploy/develop/readiness.yml
/etc/spiritvpn/deploy/develop/known_hosts
```

Use `examples/fleet-executor-bootstrap.yml` and
`examples/fleet-executor-readiness.yml` as starting points. `known_hosts` must
contain independently verified keys for every public bootstrap address and
management address used by Ansible. The executor sets
`StrictHostKeyChecking=yes`; it never invokes `ssh-keyscan`.

The first node bootstrap intentionally pauses while the WireGuard public key is
registered and its CSR is signed. Add returned public certificate chains to
`spiritvpn_agent_certificate_chains`, keyed by instance ID, and resume the same
SHA. Private WireGuard and agent keys never leave their node.

## 6. GitHub readiness and deployment

For each GitHub Environment set:

- secret `PLATFORM_SSH_PRIVATE_KEY` — private half of the dedicated forced-command key;
- variable `PLATFORM_SSH_HOST` — the IP from the tracked bootstrap inventory.

Run `platform-readiness` manually. GitHub connects with the tracked pinned host
key and that workflow can execute only the root-owned readiness command. A
sealed or uninitialized Vault returns a non-zero job result; no secret values
are returned. The separate `fleet-deploy` workflow is restricted to the
environment-bound deployment command described below.

After readiness succeeds, run `fleet-deploy` with a full SHA already reachable
from `main`. Start with `mode=dry-run`; choose `mode=apply` only after reviewing
the impact and approving the GitHub Environment. The dry-run creates and pins a
deployment record/revision, so the following apply of the same SHA must set
`resume=true`. Set `initial=true` while `refs/deployments/<environment>` remains
absent, and `allow_destructive=true` only for an explicitly reviewed destructive
plan. A different SHA is always a separate deployment and must not use resume.

The hosted runner sends an exact Git bundle but no secrets. Resolution and
Ansible execution happen on the management VPS. Current coordinator behavior is
to stop at `WAITING_FOR_BACKEND`; it does not apply backend/DNS changes or move
the deployment ref.

## 7. Snapshot and recovery drill

Create a Raft snapshot after initial import and after material secret changes:

```bash
ssh -t deploy@MANAGEMENT_HOST \
  sudo /usr/local/sbin/spiritvpn-vault-operator snapshot
```

Move the resulting root-readable snapshot immediately to encrypted external
backup storage, verify it, and remove the on-host copy according to the approved
retention procedure. A restore drill on a disposable Vault is required before
production use.

## Recovery boundary

Loss of the management VPS requires a new bootstrap, a verified Vault snapshot
restore, restoration of `/var/lib/spiritvpn/fleetctl`, and recreation/rotation
of executor AppRole credentials. Snapshots are manual in v1; without a tested
restore and backup of the manifest revision allocator this foundation is not
production-ready.
