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

## 3. Secrets: SOPS + Vault, never plaintext in Git

- **In Git, encrypted:** inventory secrets (REALITY keys, passwords, UUIDs) via
  **SOPS** (`.sops.yaml` already targets the sensitive fields). Commit ciphertext;
  distribute the age key out of band.
- **In Vault:** shared recoverable material + (future) the SSH CA.
- **Never in Git:** SSH/WireGuard **private** keys, Vault unseal keys/root token,
  `.local-secrets/`. These are gitignored; keep them that way.

### Wiring SOPS (one-time)

```bash
sudo apt install -y age sops
age-keygen -o ~/.config/sops/age/keys.txt          # each operator + the deploy machine
# put every operator's PUBLIC age recipient in .sops.yaml (replace the placeholder),
# then encrypt the sensitive inventory:
sops --encrypt inventories/prod/inventory.yml > inventories/prod/inventory.sops.yml
# add a decrypt step (or community.sops lookup) to the deploy flow; keep the
# plaintext inventory.yml gitignored.
```

## 4. Onboard / offboard an operator

The new operator runs **on their own machine** (nothing private ever leaves it):

```bash
sudo apt install -y openssh-client wireguard-tools age
ssh-keygen -t ed25519 -C "alice@spirit-ops" -f ~/.ssh/spirit_ops
umask 077; wg genkey > ~/spirit_wg.key; wg pubkey < ~/spirit_wg.key > ~/spirit_wg.pub
age-keygen -o ~/.config/sops/age/keys.txt
```

They then open a **PR** adding their three PUBLIC parts:

| Public part | Goes to |
|---|---|
| `~/.ssh/spirit_ops.pub` | `operators:` in `inventories/prod/group_vars/all.yml` |
| `~/spirit_wg.pub` (+ a free `10.20.0.x`) | `management_wireguard_external_peers` |
| `age1...` recipient | `.sops.yaml` (then `sops updatekeys` on the encrypted files) |

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

### ⚠ Operator roster decision (unresolved)

Today `control-1` and `exit-fr` trust one operator key (`xvpaul@github.com`);
`entry-1` **also** trusts `romanrabinkin@lenza`. The committed `operators` roster
currently lists only `pavel`, so enabling exclusive management would **remove
Roman from entry-1**. Decide first: is Roman a **fleet-wide** operator (add his
key to `operators`), **entry-1 only** (use a `host_vars/entry-1` override), or
should the key be **pruned**? Resolve this before running `access.yml` fleet-wide.

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
