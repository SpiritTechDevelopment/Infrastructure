# Dedicated self-hosted deployment runner

This procedure installs one persistent repository-level GitHub Actions runner
on a dedicated Debian or Ubuntu VPS. The runner may temporarily serve both the
`develop` and `prod` environments. Pull-request CI remains on GitHub-hosted
runners; only manually approved deployment workflows may use this host.

The runner is not the management host. It receives an environment-scoped SSH
identity from GitHub and may invoke only the management host's existing
`github-deploy` forced command.

```text
GitHub -- outbound HTTPS --> runner VPS -- restricted SSH --> management VPS
                                                        ├── loopback Vault
                                                        └── fleet executor
```

## Security boundary

The runner account intentionally has no `sudo`, Docker membership, Vault
credentials, or fleet SSH identity. It owns a dedicated age identity used only
to validate and update encrypted topology; this is distinct from the management
executor and recovery identities. The runner host needs outbound TCP 443 to
GitHub and outbound TCP 22 to the management host.
GitHub does not initiate an inbound connection to the runner. Limit inbound SSH
on the runner VPS to reviewed operator addresses.

A shared persistent runner is a temporary boundary: a compromised trusted job
can read infrastructure metadata and could persist and observe a later `prod`
job. It still cannot resolve Vault references. Never expose the identity to a
pull-request job; allow decryption only for trusted `main` and release workflows,
and later split the environments or move to ephemeral runners.

## Install

In the repository settings open **Settings → Actions → Runners → New
self-hosted runner → Linux**. Record the current runner version and the SHA256
shown by GitHub for the server architecture. The bootstrap refuses an
unverified archive and deliberately has no implicit `latest` mode.

Copy the reviewed script to the new runner VPS:

```bash
scp scripts/bootstrap-self-hosted-runner.sh root@RUNNER_IP:/root/
```

Generate a short-lived repository registration token locally and feed it to
the remote script over standard input. Replace the version and checksum with
the exact values shown by GitHub:

```bash
gh api \
  --method POST \
  repos/SpiritTechDevelopment/Infrastructure/actions/runners/registration-token \
  --jq .token |
ssh root@RUNNER_IP \
  '/root/bootstrap-self-hosted-runner.sh \
    --repository-url https://github.com/SpiritTechDevelopment/Infrastructure \
    --runner-version 2.REPLACE.REPLACE \
    --runner-sha256 REPLACE_WITH_64_HEX_DIGEST \
    --runner-name spiritvpn-deploy-1 \
    --labels spiritvpn-deploy'
```

The script:

- supports Debian/Ubuntu on x86-64 and arm64;
- creates the system account `github-runner`;
- installs into `/opt/actions-runner`;
- verifies the official archive before extraction;
- registers only the requested repository and label;
- installs and starts the runner as a systemd service;
- leaves runner software auto-update enabled;
- is idempotent after successful registration.

The short-lived registration token is necessarily supplied to GitHub's
`config.sh` process briefly, but is not written to a file by the bootstrap.

## Create the topology identity

After registration, dispatch `runner-sops-bootstrap`. It downloads the pinned
SOPS and age releases, verifies their SHA-256 digests, creates the identity in
the runner account's home, and prints only its public recipient. The operation
is idempotent and never replaces an existing identity.

Add that public recipient to the topology creation rule in `.sops.yaml`. The
private file remains local to the runner and must never be copied into a GitHub
secret, repository artifact, cache, or log.

## Enroll the management overlay from Git

The runner WireGuard peer is part of the encrypted platform contract, not a
dynamic peer created by a command copied to the hub. In
`inventories/bootstrap/platform.sops.yml`, pin the reviewed hub public key and
add one `platform_wireguard_runner_peers` item containing the logical runner ID,
environment, local interface, operator-range `/32`, keepalive and an initially
empty `public_key`. This is an explicit enrollment ceremony; audit output must
never populate these fields automatically.

Commit the pending declaration. From that clean exact-SHA checkout, materialize
a temporary private plan:

```bash
umask 077
python3 scripts/platform-sops.py runner-plan \
  --bundle inventories/bootstrap/platform.sops.yml \
  --runner-id RUNNER_ID \
  --source-git-sha "$(git rev-parse HEAD)" \
  --output /tmp/spiritvpn-runner-plan.yml
```

The plan contains operational addresses and pins. Never commit it or attach it
to a workflow artifact. Transfer it and the reviewed enrollment script from the
same SHA to the runner over the operator channel; keep the plan mode `0600`,
then run:

```bash
sudo scripts/enroll-runner-overlay.sh \
  --plan /tmp/spiritvpn-runner-plan.yml \
  --mode check
```

For an already enrolled runner, `check` accepts only an exact semantic match
and prints its public key while the declaration is pending. For a new runner,
use `--mode apply`; it generates the private key locally and never sends it to
Git or the hub. Put only the printed public key into the same encrypted runner
declaration and commit it.

Apply that access-boundary change with the guarded operator refresh. The
platform role renders the runner into the Git-owned base configuration and
removes only a same-ID legacy dynamic fragment. It does not import arbitrary
peer files:

```bash
scripts/platform-bootstrap.sh --reuse-tunnel --apply
```

Generate a fresh plan from the committed SHA and repeat `--mode check`. Remove
every temporary plaintext plan after use. Future check runs are read-only and
also require the runner service and interface to be active.

## Verify

The GitHub runner page must show `spiritvpn-deploy-1` as **Idle** with the
`spiritvpn-deploy` label. On the host:

```bash
cd /opt/actions-runner
sudo ./svc.sh status
sudo -u github-runner test ! -r /root
id github-runner
```

The account must not belong to `sudo`, `docker`, `lxd`, or `wheel`.

## Workflow routing

`.github/workflows/ci.yml` remains on `ubuntu-latest`. The deployment jobs in
`platform-readiness.yml`, `platform-deploy.yml`, and `fleet-deploy.yml` use:

```yaml
runs-on: [self-hosted, linux, spiritvpn-deploy]
```

Both GitHub Environments retain separate values for:

```text
PLATFORM_SSH_PRIVATE_KEY
PLATFORM_SSH_HOST
PLATFORM_SSH_KNOWN_HOSTS
```

The management firewall restricts direct SSH to `RUNNER_IP/32`; operators use
the WireGuard interface created during platform bootstrap, so laptop public addresses are
not tracked. Never place Vault, Xray, fleet Ansible, recovery, or management
executor private keys on the runner VPS. The dedicated topology identity is
the only decryption credential assigned to it.
