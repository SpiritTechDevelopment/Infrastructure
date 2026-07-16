# CI runner cutover: GitHub-hosted → self-hosted (on the overlay)

The interim `deploy.yml` runs on a **GitHub-hosted** runner that joins the
WireGuard overlay using a **disposable CI WireGuard peer** and decrypts secrets
with a **disposable CI age key** — so during this phase those keys, plus a CI SSH
key, live in **GitHub Actions secrets**. That is a deliberate, temporary tradeoff
(see the rationale in `OPERATIONS.md` §7). This file is the exact switch.

## Why cut over

A self-hosted runner that is a permanent overlay peer keeps every fleet key **on
your own infrastructure** — nothing long-lived sits in GitHub's cloud. Put it on
a **dedicated small VM**, NOT on `control-1` (co-locating CI code execution with
Vault + the hub is the worst blast radius).

## The config change (small)

```diff
# .github/workflows/deploy.yml
- runs-on: ubuntu-latest          # INTERIM
+ runs-on: [self-hosted, vpn]

-      - name: WireGuard up (join overlay)
-        run: |
-          printf '%s\n' "${{ secrets.CI_WG_CONF }}" | sudo tee /etc/wireguard/wg0.conf ...
-          sudo wg-quick up wg0
   ...
-      - name: WireGuard down
-        if: always()
-        run: sudo wg-quick down wg0 || true
```

The self-hosted runner is already on the overlay, so the WireGuard bring-up/tear-
down steps are deleted. The `make deploy / reconcile / verify / e2e` steps are
unchanged. Keep the SSH-key and age-key steps only if you store those on the
runner as secrets; the cleaner option is to place them as files on the runner
host and drop the secret-injection steps entirely.

## The runner host setup (one-time)

1. Small dedicated VM (not control-1). Install the GitHub Actions runner, labelled
   `self-hosted, vpn`. Register it **scoped to this private repo only**.
2. Make it a WireGuard peer: generate a keypair on the VM, add its **public** key
   to `management_wireguard_external_peers` + the hub, bring up `wg0`.
3. Give it an SSH key whose **public** half is an `operators` entry (deploy access).
4. Place the age key at `~/.config/sops/age/keys.txt` on the runner.
5. (Later) replace stored keys with **GitHub OIDC → Vault**: the runner federates
   to Vault per job and fetches short-lived SSH certs + secrets, so nothing
   long-lived sits on the runner at all. Needs Vault prod + the SSH CA.

## Rotate every CI-phase secret (NON-NEGOTIABLE)

A secret that has sat in GitHub Actions cannot be un-exposed. Retire all three CI
keys — do **not** reuse them on the runner:

- [ ] **CI WireGuard peer**: remove its peer from the hub + `management_wireguard_external_peers`; the runner uses a brand-new keypair.
- [ ] **CI age key**: remove its recipient from `.sops.yaml`, run `sops updatekeys` on all encrypted files (so the old key can no longer decrypt), and **rotate the underlying secrets** it could read (Grafana pw, REALITY keys, TLS key) if you consider the exposure material.
- [ ] **CI SSH key**: remove its pubkey from `operators`, run `playbooks/access.yml` (dry-run first), confirm it's gone from every host's `authorized_keys`.
- [ ] Delete the now-unused `CI_WG_CONF`, `CI_AGE_KEY`, `CI_SSH_KEY` from GitHub Environment secrets.

## Harden the runner regardless of type

- Private repo; **disable fork-PR execution** (self-hosted runners run untrusted
  PR code by default — the #1 footgun).
- Deploy gated to `main` + the `production` Environment with **required reviewers**.
- Dedicated non-login runner user; reconcile runtime users after any deploy.
- Pin all third-party Actions to commit SHAs.
