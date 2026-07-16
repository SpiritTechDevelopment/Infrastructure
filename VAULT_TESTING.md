# Test Vault and the backend AppRole

Run these checks after `vault-init.sh`, three successful unseal operations and
`vault-bootstrap.sh`.

## 1. Check Vault status

```bash
ssh ubuntu@PLATFORM_SSH_IP \
  'sudo docker compose -f /opt/vault/compose.yml exec -T \
   -e VAULT_ADDR=https://127.0.0.1:8200 \
   -e VAULT_CACERT=/vault/tls/vault.crt \
   vault vault status'
```

Expected: `Initialized true` and `Sealed false`.

## 2. Write a smoke application secret

Open an interactive root shell on the platform and avoid putting the root token into shell
history:

```bash
ssh -tt ubuntu@PLATFORM_SSH_IP
sudo -i
read -rsp 'Temporary Vault root token: ' VAULT_TOKEN; echo
export VAULT_TOKEN
export VAULT_ADDR=https://127.0.0.1:8200
```

Use the Vault CLI inside the container:

```bash
v() {
  docker compose -f /opt/vault/compose.yml exec -T \
    -e VAULT_ADDR="$VAULT_ADDR" \
    -e VAULT_CACERT=/vault/tls/vault.crt \
    -e VAULT_TOKEN="$VAULT_TOKEN" \
    vault vault "$@"
}

v kv put secret/backend/smoke value=ok
v kv get secret/backend/smoke
```

## 3. Obtain one-time AppRole credentials

```bash
ROLE_ID="$(v read -field=role_id auth/approle/role/backend/role-id)"
SECRET_ID="$(v write -field=secret_id -f auth/approle/role/backend/secret-id)"
printf 'ROLE_ID=%s\n' "$ROLE_ID"
```

The SecretID expires after ten minutes and can be used once.

## 4. Log in as the backend role

```bash
BACKEND_TOKEN="$(
  v write -field=token auth/approle/login \
    role_id="$ROLE_ID" secret_id="$SECRET_ID"
)"
```

Confirm that the backend role can read its namespace:

```bash
docker compose -f /opt/vault/compose.yml exec -T \
  -e VAULT_ADDR="$VAULT_ADDR" \
  -e VAULT_CACERT=/vault/tls/vault.crt \
  -e VAULT_TOKEN="$BACKEND_TOKEN" \
  vault vault kv get secret/backend/smoke
```

Confirm it cannot write:

```bash
docker compose -f /opt/vault/compose.yml exec -T \
  -e VAULT_ADDR="$VAULT_ADDR" \
  -e VAULT_CACERT=/vault/tls/vault.crt \
  -e VAULT_TOKEN="$BACKEND_TOKEN" \
  vault vault kv put secret/backend/should-fail value=no
```

The final command must return a permission-denied error.

Clean up and remove tokens from the shell:

```bash
v kv delete secret/backend/smoke
unset SECRET_ID BACKEND_TOKEN ROLE_ID VAULT_TOKEN
exit
```

This AppRole is only a bootstrap identity. Before deploying the real backend, define its
exact secret paths, token renewal behavior and SecretID delivery method.
