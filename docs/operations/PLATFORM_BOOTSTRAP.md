# Management platform bootstrap

This operator procedure installs or reconciles Vault without changing its
initialization or seal state and installs a restricted GitHub SSH command gate.
It does not deploy fleet nodes.

## 1. Prepare the host

Create one management VPS manually. Independently obtain its public SSH host
key from the provider console or another trusted channel; do not discover trust
with `ssh-keyscan` during deployment.

The reviewed one-host inventory, pinned host key and non-secret Ansible
bootstrap variables are tracked together in
`inventories/bootstrap/platform.sops.yml`. The `inventory`, `known_hosts` and
`vars` values are SOPS ciphertext; their structure and values do not appear in
Git. Edit the bundle only through SOPS:

```bash
sops inventories/bootstrap/platform.sops.yml
```

The first connection to a clean management VPS uses its public address from
the encrypted inventory and its independently pinned host key. The bundle also
carries operator SSH public keys, develop/prod handoff public keys, the
runner's public `IP/32`, management WireGuard addresses, and operator
WireGuard public peers. Operator laptop public addresses are not needed. Never
decrypt the bundle into a persistent file or use `sops --decrypt --in-place`.

Each operator creates a WireGuard key locally before bootstrap. Only the
public key and its reserved `/32` addresses enter the SOPS bundle; the private
key remains on that operator's device:

```bash
umask 077
wg genkey > ~/.config/spiritvpn/keys/operator-wg.key
wg pubkey < ~/.config/spiritvpn/keys/operator-wg.key
```

`fleet-platform-check` rejects an empty operator peer roster, so the final
firewall cannot be applied before a tested recovery path has been declared.

The Vault image is public configuration pinned by both version and digest in
`roles/platform_vault/defaults/main.yml`. Upgrade it through an ordinary
reviewed pull request; it does not belong in SOPS.

The matching age private identity is an operator/recovery credential. It must
remain outside the repository and be backed up in approved offline recovery
storage. The public recipient and SOPS policy live in `.sops.yaml`.

Use a different GitHub SSH key pair for each environment. Add each public half
to `platform_github_ssh_keys` with its environment binding; store the matching
private half only in that GitHub Environment. The forced command receives the
binding from root-owned `authorized_keys`, not from workflow input.

## 2. Validate and apply

Create the ignored operator environment from the reviewed version constraints:

```bash
python3 -m venv ansible-env
ansible-env/bin/pip install -r requirements-ansible.txt
export PATH="$PWD/ansible-env/bin:$PATH"
ansible --version  # must report ansible-core 2.18.x
```

WireGuard is not a prerequisite. The role installs it, generates the management
private key on that VPS, configures the environment hub addresses and reviewed
operator peers, starts `wg-quick`, and only then installs the final firewall.
The management private key never leaves the VPS.

The recommended operator entry point runs local tests, lint, SOPS validation,
pinned SSH preflight, an explicit confirmation, bootstrap and convergence
verification in one command:

```bash
scripts/platform-bootstrap.sh --apply
```

Run it without `--apply` to execute the complete non-mutating gate only:

```bash
scripts/platform-bootstrap.sh
```

For diagnosis, the same stages remain available as lower-level targets:

```bash
make fleet-platform-check
make fleet-platform-bootstrap-check CONNECT=1
make fleet-platform-bootstrap APPLY=1
```

Each target decrypts into a fresh mode-`0700` temporary directory, materializes
mode-`0600` inventory/known-hosts/vars files, runs validation or Ansible, and
removes plaintext on exit. Override `PLATFORM_BUNDLE` only for an explicitly
reviewed alternative encrypted bundle.

`fleet-platform-bootstrap-check` verifies parsing, syntax, pinned SSH trust and
connectivity; a clean-host check cannot predict files and keys that do not yet
exist. Keep a provider console open for the first apply.

`fleet-platform-bootstrap` is a two-phase orchestrator. It first installs the
management WireGuard role while public SSH remains open, receives only the hub
public metadata, matches the local private key to the encrypted operator
roster, installs `/etc/wireguard/spiritvpn-mgmt.conf` through `sudo`, starts the
local tunnel and verifies pinned SSH through it. Only after that succeeds does
it apply the firewall, Docker, Vault and executors through the tunnel. It then
runs the same playbook again to verify convergence. It refuses to overwrite an
unmanaged local WireGuard config.

The role hardens SSH/firewall, installs Docker, generates a host-local transport
CA and Vault certificate, starts loopback-only Vault, and installs the
`github-deploy` forced command. Private TLS keys never leave the host.

### Changing a bundle value after the hub is hardened

The first phase is unreachable once the hub is hardened: its public SSH is
closed by design, so `fleet-platform-bootstrap` would hang on the reachability
check. CI cannot carry the value either — the `github-deploy` forced command
parses a fixed argument list and refuses everything else, and that is the point
of it.

Edit the value in the encrypted bundle, commit it, then deliver it through the
tunnel the first phase already built:

```bash
scripts/platform-bootstrap.sh --reuse-tunnel --apply
# or, without the repository-wide gate:
make fleet-platform-refresh APPLY=1 \
  PLATFORM_BUNDLE=inventories/bootstrap/platform.sops.yml \
  PLATFORM_WIREGUARD_PRIVATE_KEY="$HOME/.config/spiritvpn/keys/operator-wg.key"
```

This reuses `/etc/wireguard/spiritvpn-mgmt.conf` instead of rewriting it, takes
the hub's public endpoint from that file rather than guessing it, and refuses a
config it did not write, one that points at a different host than the bundle
describes, or an interface that is down. It applies the same playbook the second
phase applies, but as `deploy_mode: hardened`, so the hub does not fall back to
the wider bootstrap port set. The hub rewrites
`/etc/spiritvpn/platform/runtime-vars.yml` as the record of the explicitly
applied access contract. A later `platform-deploy` decrypts the exact bundle
from its reviewed Git SHA and refuses apply if that contract does not match.
The file is therefore an approval boundary, not a second desired-state source.

A new key must be added in two places or it silently goes nowhere:
`EXPECTED_VARIABLE_KEYS` in `scripts/platform-sops.py`, which validates the
bundle, and the persisted allow-list in `roles/platform_executor`, which decides
what the hub writes down. These sets must match exactly apart from the derived
public endpoint; a unit test fails if either side outgrows the other. Management
WireGuard interface, environment networks, listen port and MTU belong to this
encrypted contract and deliberately have no usable role fallback.

The same contract may declare management runner peers. A runner with an empty
public key is pending and does not change the hub. Once its locally generated
public key is reviewed into SOPS, the platform role owns that peer in the base
configuration and removes only the matching legacy dynamic fragment. The
temporary runner plan is projected from a clean exact-SHA checkout and must
never be committed in plaintext.

## 3. Vault ceremony

The bootstrap installs `/usr/local/sbin/spiritvpn-vault-operator`. It is the
only supported manual interface for the initial ceremony; run it from the
operator account through `sudo`. It never stores an unseal share or root token.

For a new installation, first confirm that Vault is reachable and uninitialized:

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

## 5. Install the remaining protected executor inputs

Platform reconciliation owns this root-owned file and rewrites it from the
reviewed SOPS bundle plus the hub's local WireGuard public key:

```text
/etc/spiritvpn/deploy/develop/bootstrap.yml
```

Do not install or edit it manually. Agent certificate chains are generated in
a temporary protected file by the coordinator when it signs a new node CSR.

The only remaining operator-provided readiness input is:

```text
/etc/spiritvpn/deploy/develop/readiness.yml
```

Use `examples/fleet-executor-readiness.yml` as the starting point.
`examples/fleet-executor-bootstrap.yml` documents the generated shape only.
`known_hosts` is compiled from each environment's SOPS topology for the exact
Git SHA; the executor sets `StrictHostKeyChecking=yes` and never invokes
`ssh-keyscan`.

The environment CA root is a protected input too, because the coordinator signs
agent CSRs where it runs:

```text
/etc/spiritvpn/deploy/develop/ca/develop/{ca.crt,ca.key}
```

Per-environment on purpose: the develop executor must not be able to read the
prod root. The inner `develop/` is the CA adapter's own layout, so the
environment name appears twice. Without it, an apply that bootstraps nodes stops
before touching any machine.

Node bootstrap is two-phase and automatic: the coordinator collects every CSR
first (`playbooks/bootstrap/csr.yml`), signs them against that root, and only
then runs the bootstrap that installs the returned chains. WireGuard peer
registration on the management hub is automatic as well. Private WireGuard and
agent keys never leave their node.

A hand-run `make fleet-bootstrap` is still single-pass: the role reports the CSR
in its output, the operator signs it with `make fleet-pki-sign` and repeats the
run with `spiritvpn_agent_certificate_chains` filled in.

## 6. GitHub readiness and deployment

For each GitHub Environment set all three values as secrets:

- secret `PLATFORM_SSH_PRIVATE_KEY` — private half of the dedicated forced-command key;
- secret `PLATFORM_SSH_HOST` — management address;
- secret `PLATFORM_SSH_KNOWN_HOSTS` — complete pinned management host-key line.

Run `platform-readiness` manually. GitHub connects with the tracked pinned host
key and that workflow can execute only the root-owned readiness command. A
sealed or uninitialized Vault returns a non-zero job result; no secret values
are returned. The `platform-deploy`, `control-deploy` and `fleet-deploy`
workflows are restricted to their environment-bound commands.

After the first bootstrap, management component changes use the normal Git
flow. Merge the reviewed pull request to `main`, copy the resulting full
40-character commit SHA, then dispatch `platform-deploy` first with
`mode=check` and then with `mode=apply`. The runner sends only an exact Git
bundle. The management host runs `playbooks/platform/steady.yml` locally with
the root-owned runtime variables persisted during bootstrap; GitHub receives
no SOPS identity or Vault credential.

After `Environment.spec.control` contains reviewed image digests and all listed
control references exist in Vault, dispatch `control-deploy` for the same exact
infrastructure SHA: first `mode=check`, then `mode=apply`. The management host
locally reconciles the environment-specific PostgreSQL, migrations and backend;
no direct backend SSH is involved. For an existing production database, put a
reviewed absolute external backup command argv in the SOPS-encrypted
`Environment.spec.control.postgres.external_backup_command_argv`. Check renders
that Git-owned policy without requiring a local file. Apply additionally
requires the root-owned `/etc/spiritvpn/deploy/prod/control.yml` approval marker
to contain the exact same argv; a mismatch stops before Ansible or migrations.

`platform-deploy` decrypts `inventories/bootstrap/platform.sops.yml` from the
exact reviewed SHA. Access-boundary changes still require the guarded
operator-controlled `fleet-platform-refresh`: apply compares Git with the
explicitly applied runtime contract and stops on a mismatch. This prevents a
routine component upgrade from silently changing SSH or WireGuard access.

After readiness succeeds, run `fleet-deploy` with a full SHA already reachable
from `main`. Start with `mode=dry-run`; choose `mode=apply` only after reviewing
the impact and approving the GitHub Environment. The dry-run creates and pins a
deployment record/revision, so the following apply of the same SHA must set
`resume=true`. Set `initial=true` while `refs/deployments/<environment>` remains
absent, and `allow_destructive=true` only for an explicitly reviewed destructive
plan. A different SHA is always a separate deployment and must not use resume.

The self-hosted runner sends an exact Git bundle but no Vault secrets. Resolution and
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
