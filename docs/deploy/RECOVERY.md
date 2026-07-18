# Disaster recovery — surviving a lost laptop

Today, several crown-jewel keys live **only** on the operator workstation. If that
laptop dies, you lose the ability to decrypt secrets, SSH in, join the overlay, and
unseal Vault — potentially unrecoverable. This makes those keys **durable** by
storing them in the repo, **passphrase-encrypted**, so:

- **Laptop dies** → clone the repo on a new machine + your passphrase → back in.
- **GitHub account compromised** → the attacker gets only an opaque `.age` blob they
  cannot open without your passphrase. Not fatal.

The passphrase is the one secret that lives **only in your head** (plus, ideally, a
physically separate written copy — a safe, not the repo).

## What's in the bundle

`scripts/recovery-bundle.sh` packages the private material that exists on your
machine:

| File | Grants |
|---|---|
| `~/.config/sops/age/keys.txt` | decrypts every SOPS secret |
| `~/.ssh/spirit_ops` | SSH into the fleet |
| `~/spirit_wg.key` | WireGuard overlay identity |
| `.local-secrets/vault-init.json` | Vault unseal keys + root token |

Everything else you need to operate is already in the repo (encrypted secrets,
playbooks, the public-key roster).

## Create / update the bundle

```bash
bash scripts/recovery-bundle.sh            # prompts for a STRONG passphrase (memorize it)
git add recovery/$(id -un)-recovery.age
git commit -m "recovery: update bundle" && git push
```

Only the encrypted `recovery/*.age` blob is committed — `.gitignore` blocks any
decrypted/extracted material in `recovery/`. Re-run after rotating any key.

## Restore on a new machine

```bash
git clone <repo> && cd infra_v1
bash scripts/recovery-restore.sh recovery/<you>-recovery.age
# enter your passphrase; files return to their original paths.
sops -d inventories/prod/secrets.sops.yml >/dev/null && echo "age key OK"
```

If the new machine uses a different username/home, restore to a staging dir and
move the files yourself: `bash scripts/recovery-restore.sh recovery/<you>-recovery.age ./restore-review`.

## Rules

- **Strong passphrase, memorized.** A long diceware phrase; scrypt makes brute-force
  infeasible only if the passphrase is strong. Keep a physical copy somewhere safe —
  never in the repo.
- **Never commit the passphrase or any decrypted material.** Only `*.age` blobs.
- **Rotate after exposure.** If the passphrase might be compromised, rotate the
  underlying keys (new age key + `sops updatekeys`, new SSH key + `access.yml`,
  new WG key + hub peer) and re-bundle. Git history keeps old ciphertext, so treat
  a compromised passphrase as exposure of everything in past bundles.

## How this fits the access model

This is a **personal, per-operator** durability backup — it is not a shared store
and does not change the access model in `OPERATIONS.md`. For the *team* recovery
path (a locked-out operator with no bundle) and the org break-glass (provider
console, static break-glass key, Vault unseal keys), see `OPERATIONS.md` §8.
