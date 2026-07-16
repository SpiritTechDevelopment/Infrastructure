# Local secrets and state examples

Copy only the files you need into `.local-secrets/`. That directory is ignored and must
never be committed or included in support archives.

Normal production deployment expects:

```text
grafana-admin-password.txt
vmshare.ru-fullchain.pem
vmshare.ru-privkey.pem
```

`acme.yml.example` is for the separate certificate-issuance play. The desired-users JSON
is an example backend reconciliation state file, not an infrastructure secret store.

Rotate any credentials that appeared in earlier repository copies.
