# First deployment

## 1. Prepare the controller

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install 'ansible-core>=2.18,<2.19' ansible-lint yamllint
make deps
```

## 2. Supply local secrets

```bash
mkdir -p .local-secrets
chmod 700 .local-secrets
printf '%s\n' 'REPLACE_WITH_A_LONG_RANDOM_PASSWORD' > .local-secrets/grafana-admin-password.txt
cp /path/to/fullchain.pem .local-secrets/vmshare.ru-fullchain.pem
cp /path/to/privkey.pem .local-secrets/vmshare.ru-privkey.pem
chmod 600 .local-secrets/*
```

The certificate must match every configured `reality_server_names` value. The preflight
script validates the PEM pair and hostname coverage before any remote changes.

## 3. Verify inventory and SSH

Review `inventories/prod/inventory.yml`, including public addresses, SSH ports, DNS names,
REALITY short IDs, service UUIDs, and `entry_default_exit_tag`.

```bash
make inventory

# Normal OpenSSH config/agent/default-key mode
make ping

# Force one key, avoiding MaxAuthTries failures from a busy SSH agent
make ping SSH_AUTH=key SSH_KEY="$HOME/.ssh/id_ed25519"

# Or use one shared root password for all enabled hosts
make ping SSH_AUTH=password
```

Password mode requires `sshpass` (`sudo apt install sshpass` on Ubuntu). This repository
does not create or modify SSH users, keys, or sshd configuration. Existing access must work
on every enabled server.

## 4. Deploy and prove the whole path

```bash
# Choose the same SSH_AUTH mode that passed above. Examples:
make deploy-e2e SSH_AUTH=key SSH_KEY="$HOME/.ssh/id_ed25519" 2>&1 | tee deploy-e2e.log
# make deploy-e2e SSH_AUTH=password 2>&1 | tee deploy-e2e.log
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
