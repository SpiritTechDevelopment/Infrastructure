# Operations & Access — working with this repo

How the fleet is operated through Git: who can do what, how changes ship, and how
to on/offboard an operator. For the fleet's architecture see `ARCHITECTURE.md`;
for the current hardening state see `CONVERGENCE_STATUS.md`.

## 1. Access is three independent grants

"Proper rights" is not one thing. Keep these separate so you can grant monitoring
without deploy, revoke one person without touching others, etc.

| Plane | Controls | Granted by |
|---|---|---|
| **Repo** | propose / review / merge changes | GitHub role + `main` ruleset + `.github/CODEOWNERS` |
| **Secrets** | decrypt inventory; read Vault | age recipient in `.sops.yaml`; Vault policy |
| **Hosts / overlay** | SSH in; reach Grafana/API/telemetry | SSH pubkey in `operators`; WireGuard peer |

Nobody shares private keys. Each person generates their own and you register the
**public** half.

## 2. Change workflow (GitOps)

```
feature branch ─PR─▶ ci (lint, hosted runner) ─review (CODEOWNERS)─▶ merge to main ─▶ deploy
```

- `main` is the deployed truth. Protect it: require PR, require the `ci` check,
  require ≥1 review. No direct pushes.
- **CI** (`.github/workflows/ci.yml`) is static (`make check` + `make lint`) — no
  secrets, no fleet access, safe on every PR.
- **Deploy** happens from a **trusted machine on the WireGuard overlay** (your
  workstation today; a self-hosted runner later — see `CUTOVER.md`). The audit
  trail is the merged PR (who approved, what changed).

### Deploy from a workstation

```bash
cd infra_v1 && source ../../venv/bin/activate     # ansible-core 2.18.x
sudo wg-quick up wg0                               # must be an overlay peer
make check                                         # optional local pre-flight
make deploy                                        # site.yml: preflight→platform→exits→wire→entries→verify
make reconcile NODE=entry-1 STATE=/secure/desired-users.json   # after any xray restart
```

> A full `make deploy` can restart the `vpn` containers, which **wipes in-memory
> runtime users**. Always `reconcile` afterwards. To change *only* operator SSH
> access, do NOT use `make deploy` — use the scoped play in §4.

## 3. Secrets: SOPS-encrypted in Git, decrypted at deploy time

Deploy secrets live **encrypted in the repo** (`inventories/prod/secrets.sops.yml`,
committed) and are materialized locally with **`make decrypt`** (writes the
gitignored `secrets.plain.yml`, which the deploy targets pass to Ansible as
extra-vars). This replaced the hand-placed `.local-secrets/` files.

```bash
make decrypt        # sops -d secrets.sops.yml -> secrets.plain.yml (needs your age key)
make deploy         # depends on decrypt; passes the secrets as --extra-vars
sops inventories/prod/secrets.sops.yml   # edit/add a secret (re-encrypts on save)
```

What lives where — the important split:

| Material | Home | In Git? |
|---|---|---|
| grafana pw, TLS **private** key, REALITY keys, UUIDs | `secrets.sops.yml` (SOPS) | ✅ ciphertext |
| TLS certificate (public) | `secrets.sops.yml` | ✅ plaintext (it's public) |
| operator age / SSH / WireGuard **private** keys | each operator's machine | ❌ never |
| per-node SSH host keys, node WireGuard private keys | on the node | ❌ never |
| **Vault unseal keys + root token** (`vault-init.json`), vault TLS key | **out-of-band** (password manager / safe) | ❌ never, even encrypted |

> `.local-secrets/` today still holds break-glass material (`vault-init.json`,
> `wireguard/`). That is **out-of-band** material — move it to a password manager
> or offline store; do **not** migrate it into SOPS/Git. Only deploy secrets go in
> SOPS.

### First-time SOPS setup (per operator)

```bash
sudo apt install -y age sops
age-keygen -o ~/.config/sops/age/keys.txt      # prints your age PUBLIC key: age1...
# add your (and each operator's) age PUBLIC key to .sops.yaml, then re-wrap:
sops updatekeys inventories/prod/secrets.sops.yml
```

Back up your age **private** key (`~/.config/sops/age/keys.txt`) out of band — if
every recipient loses their key, the secrets are unrecoverable.

## 4. Onboard / offboard an operator

The new operator runs **on their own machine** (nothing private ever leaves it):

```bash
sudo apt install -y openssh-client wireguard-tools age
ssh-keygen -t ed25519 -C "alice@spirit-ops" -f ~/.ssh/spirit_ops
umask 077; wg genkey > ~/spirit_wg.key; wg pubkey < ~/spirit_wg.key > ~/spirit_wg.pub
age-keygen -o ~/.config/sops/age/keys.txt
```

They then open a **PR** adding their PUBLIC keys as **one operator block** in
`inventories/prod/group_vars/all.yml`:

```yaml
operators:
  - name: alice
    ssh_key: "ssh-ed25519 AAAA… alice"        # -> root authorized_keys (access.yml)
    wg_pubkey: "…="                            # -> overlay peer on the hub
    wg_ip: "10.20.0.4"                         # a free 10.20.0.x
    age_recipient: "age1…"                     # ALSO add to .sops.yaml (see below)
```

Because `sops` reads `.sops.yaml` (not Ansible vars), also add their `age1…` to
`.sops.yaml` and run `sops updatekeys inventories/prod/secrets.sops.yml` so they can
decrypt. (SSH-only operators can leave `wg_*`/`age_recipient` empty.)

You review + merge, then apply the **scoped, non-disruptive** access play:

```bash
# authorized_keys only — no packages, no services, no data-plane impact.
ansible-playbook -i inventories/prod/inventory.yml playbooks/access.yml --check --diff   # READ the diff
ansible-playbook -i inventories/prod/inventory.yml playbooks/access.yml                   # apply
```

`playbooks/access.yml` manages `authorized_keys` **exclusively** (the file becomes
exactly the `operators` roster) and refuses to write an empty/invalid list. Since
SSH is key-only fleet-wide, **always dry-run and read the diff** — a missing key
locks that person (or everyone) out; the provider console is the only fallback.

You send the operator back three non-secret facts to build their `wg0.conf`: the
hub public key (`REiFPldc8cv5rQeE0i4rFEumslWT/zbJvZHCG21q/2I=`), the hub endpoint
(`193.247.81.167:51820`), and their assigned overlay IP.

**Offboard** = reverse the PR (drop their `operators`/peer/recipient entries),
`sops updatekeys`, run `access.yml`, and **rotate any shared secret they could
decrypt**.

### Operator roster

Current fleet operators (trusted for root login on **every** host): `pavel`
(`xvpaul@github.com`) and `roman` (`romanrabinkin@lenza`). Enabling exclusive
management adds Roman to `control-1` + `exit-fr` (he was previously only on
`entry-1`) — an intentional grant, so Roman now has access to the Vault host too.

## 5. Add a new server

Per `ONBOARDING_AND_HARDENING.md` §4: add the host to the (encrypted) inventory +
a `host_vars/<host>/firewall.yml` profile + a WG peer, with `node_enabled: false`
→ run the `deploy_mode: bootstrap` play → verify → flip `node_enabled: true` in a
follow-up PR. Firewall profiles ARE committed (non-secret); model on
`host_vars/entry-1/firewall.yml`.

## 6. Monitor the fleet

Join the overlay (`wg-quick up wg0`), then:

- Grafana `http://10.20.0.1:3000` (your own Grafana user — not the admin login)
- Prometheus `http://10.20.0.1:9090`, Loki `http://10.20.0.1:3100` (header
  `X-Scope-OrgID: ops`)

Monitoring needs only an **overlay peer + a Grafana account** — no repo access.

## 7. CI/CD notes

- `ci.yml` = lint, hosted runner, no secrets. Enable now.
- `deploy.yml` = an **interim** hosted-runner deploy that joins the overlay via a
  disposable CI WireGuard peer + CI age key held in GitHub secrets. It's optional;
  workstation deploys need none of that. Before relying on either pipeline, **pin
  the actions to commit SHAs** and set required reviewers on the `production`
  Environment. To move to a self-hosted runner (and drop the cloud secrets), see
  `CUTOVER.md`.

## 8. Losing your keys — recovery

**Principle: GitHub is the recovery *coordination* layer, not a key store.**
GitHub access alone must never equal infra access — otherwise one compromised
GitHub account = the whole fleet. So GitHub privileges let a locked-out operator
*propose* new keys and *read* encrypted secrets, but **not decrypt or SSH in by
themselves**. That separation is deliberate.

If you lose your SSH / WireGuard / age keys:

1. Regenerate all three keypairs locally (§4).
2. Using your **GitHub** access, open a PR replacing your old public keys with the
   new ones (`operators`, `management_wireguard_external_peers`, `.sops.yaml`).
3. **Another operator** reviews + merges, then runs `sops updatekeys` (re-wraps the
   secrets to your new age key — this *requires* an existing key-holder) and
   `playbooks/access.yml` (re-installs your SSH key). They re-send you the hub
   WireGuard facts.
4. You regenerate `wg0.conf`, rejoin the overlay, and you're back.

This needs a second operator on purpose: SOPS re-encryption and access grants
can't be self-served from GitHub alone — that's the security property, not a gap.

**If no second operator is available / total loss** — the break-glass paths, all
**org-held and out-of-band (never in GitHub)**:

- **Provider console / KVM** — always-on hard fallback to the hosts.
- **Static break-glass SSH key** — offline-stored, never rotated away.
- **Vault unseal keys + root token** — offline (from `vault-init.json`, which must
  leave `.local-secrets/` for a password manager / safe).
- *(Optional)* a **break-glass recovery age key** whose private half lives in that
  same offline store and is a second recipient on every SOPS file — so decryption
  is recoverable without another operator. It must stay **out of band, never in
  GitHub**, exactly like the unseal keys.

What may safely live in GitHub for recovery: the **public-key roster**, the
**SOPS-encrypted secrets** (ciphertext), and this runbook. Nothing whose mere
possession (without a separate key) grants access.
