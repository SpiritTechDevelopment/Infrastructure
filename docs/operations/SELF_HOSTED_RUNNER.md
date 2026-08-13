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
credentials, fleet SSH identity, or topology decryption key. The runner host
needs outbound TCP 443 to GitHub and outbound TCP 22 to the management host.
GitHub does not initiate an inbound connection to the runner. Limit inbound SSH
on the runner VPS to reviewed operator addresses.

A shared persistent runner is a temporary boundary: a compromised `develop`
job could persist and observe a later `prod` job. Require environment approval,
allow deployments only from `main`, and later split the environments or move
to ephemeral runners.

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
not tracked. Never place Vault, SOPS, Xray, or fleet Ansible private keys on
the runner VPS.
