# Next steps

Prioritized. The overlay-first hardening convergence is essentially complete; what
remains needs an operator decision, a maintenance window, or sudo on the workstation
— not more build. See [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md) for the decisions each
depends on.

## High priority (small, unblocks reliability)

1. ~~**Fix the workstation `wg0` so the overlay survives boot.**~~ **DONE
   (2026-07-18).** `Address = 10.20.0.2/24` under `[Interface]` and
   `wg-quick@wg0` enabled; `nmcli` shows `wg0` externally-managed (NM won't fight
   `wg-quick`). Live `wg0` still shows `/32` until the next clean bring-up — the
   `/24` takes effect on reboot or `sudo wg-quick down/up wg0`.

2. **Revoke / rotate the Vault bootstrap root token.** The root-token *file* on
   control-1 was shredded, but the token value still lives in `.local-secrets/vault-init.json`.
   If you consider it exposed (it briefly configured the CA), rotate the root token.
   Keep `vault-init.json` out-of-band only.

3. **Set up Vault auto-unseal, or a manual-unseal runbook.** Vault is unsealed now,
   but re-seals on any restart (manual 3-of-5). Anything that depends on Vault (the
   SSH CA) wants it reliably up. Decide transit/KMS auto-unseal vs. an accepted runbook
   (see OPEN_QUESTIONS #1).

## Medium priority (finish the WireGuard + SSH-CA loops)

4. **Apply the WireGuard codification to the live overlay** (currently only codified).
   The first `make management` rewrites configs to canonical form → restarts `wg-quick@wg0`
   (brief blip; data plane unaffected). Do it **in a maintenance window, over the hosts'
   public SSH, with provider console ready** (managing `wg0` over the overlay drops your
   own connection). See [WIREGUARD.md](WIREGUARD.md).

5. **Make the SSH CA usable by operators from their own machine.** Vault is loopback-only,
   so today you sign by SSHing to control-1. To sign remotely, expose Vault on the
   overlay (needs a Vault redeploy = restart = re-seal — pair with #3), then create a
   `userpass` user per operator mapped to the `ssh-operator` policy. See
   [VAULT_SSH_CA.md](VAULT_SSH_CA.md) and OPEN_QUESTIONS #2.

6. **Complete the WireGuard peer roster for Roman.** Roman can SSH/deploy but has no
   WireGuard or age key yet — he can't join the overlay or `make decrypt`. When he
   generates them: add `wg_pubkey`/`wg_ip` to `operators`, add his age recipient to
   `.sops.yaml` + `sops updatekeys`.

## Lower priority / when convenient

7. **Stand up a self-hosted CI runner** (on the overlay, a dedicated box — not control-1)
   to move deploys off the workstation with a `production` Environment + required
   reviewers, and retire the interim hosted-runner secrets. See [CUTOVER.md](CUTOVER.md).
8. **GitHub repo hygiene:** confirm the repo is **private**; add branch protection on
   `main` (require the `ci` check + review); set the real `@team` in `.github/CODEOWNERS`;
   pin the workflow Actions to commit SHAs.
9. **Rotate the workstation WireGuard private key** (older note: it may have been pasted
   into a prior chat) when convenient — regenerate on-node, swap the hub peer.
10. **`tc-shaping.sh.j2`** remains an orphaned template (traffic shaping never wired) —
    wire it or delete it.

## How to deploy (reminder)

From a workstation on the overlay: `sudo wg-quick up wg0 && make decrypt && make deploy`.
After any deploy that restarts `vpn` containers, `make reconcile` runtime users. For
firewall/access changes use the dead-man + verify discipline in
[CONVERGENCE_STATUS.md](CONVERGENCE_STATUS.md) §5.
