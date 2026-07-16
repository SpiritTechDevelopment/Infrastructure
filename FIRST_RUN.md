# First deployment

## 1. Prepare the controller

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install 'ansible-core>=2.18,<2.19' ansible-lint yamllint
make deps
```

## 2. Secrets (SOPS) + overlay

Deploy secrets (Grafana password, TLS cert/key, `entry_service_uuid`) are
**SOPS-encrypted** in `inventories/prod/secrets.sops.yml`. You need an age key that
is a recipient in `.sops.yaml` — generate one and have an operator add your public
key (`sops updatekeys`):

```bash
age-keygen -o ~/.config/sops/age/keys.txt   # first time; give the age1... PUBLIC key to an operator
sudo wg-quick up wg0                         # management is overlay-only — you must be a wg0 peer
make decrypt                                 # -> secrets.plain.yml (gitignored), consumed by deploy
```

`.local-secrets/` is only for out-of-band break-glass material (Vault unseal keys,
per-node WireGuard keys), never deploy secrets. The TLS certificate must match every
`reality_server_names` value; preflight validates the PEM pair before remote changes.
Full secrets model: [OPERATIONS.md](OPERATIONS.md) §3.

## 3. Verify inventory and SSH

Review `inventories/prod/inventory.yml`, including public addresses, SSH ports, DNS names,
REALITY short IDs, service UUIDs, and `entry_default_exit_tag`.

```bash
make inventory

# Normal OpenSSH config/agent/default-key mode
make ping

# Force one key, avoiding MaxAuthTries failures from a busy SSH agent
make ping SSH_AUTH=key SSH_KEY="$HOME/.ssh/id_ed25519"
```

SSH is **key-only fleet-wide** (password auth is disabled on every host) — your key
must be in the `operators` roster ([OPERATIONS.md](OPERATIONS.md) §4). Reach
`control-1` over the overlay (`-e ansible_host=10.20.0.1`); its public `:22`
banner-hangs from the workstation.

## 4. Deploy and prove the whole path

```bash
# make decrypt has run (step 2) and wg0 is up. Then:
make deploy-e2e SSH_AUTH=key SSH_KEY="$HOME/.ssh/id_ed25519" 2>&1 | tee deploy-e2e.log
```

The run is successful only when it reaches `E2E PASS`. Send `deploy-e2e.log` when reporting a
failure; redact addresses or identifiers only when needed, but do not remove the failing task
and its preceding context.

## 5. Add a real backend-owned user

```bash
UUID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
EMAIL="device-001@example.invalid"
make api-add NODE=entry-1 UUID="$UUID" EMAIL="$EMAIL"
make gen-client NODE=entry-1 UUID="$UUID" EMAIL="$EMAIL" OUT=device-001.json
```

The generated JSON is a local Xray client configuration. The command also prints a VLESS URI.
Persist the UUID/email mapping in the backend, because API-added users are runtime state.
