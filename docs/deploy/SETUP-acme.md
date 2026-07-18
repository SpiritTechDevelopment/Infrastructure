# ACME (certbot + Cloudflare) — setup

## 1. Create a Cloudflare API token (the one manual step)
Cloudflare dashboard -> My Profile -> API Tokens -> Create Token
-> use the "Edit zone DNS" template
-> Zone Resources: Include -> Specific zone -> vmshare.ru
-> Create, copy the token.

## 2. Put the token in the inventory (control-1 or all.vars)
    cloudflare_api_token: "PASTE_TOKEN"   # SOPS-encrypt this later
Set your notification email in roles/acme/defaults/main.yml (acme_email).

## 3. Add a Makefile target
    certs:
    	ansible-playbook playbooks/acme.yml --limit "$(or $(LIMIT),control-1)"

## 4. Point the fleet at the fetched cert (both nodes)
Replace the mask_tls_* fields on exit-1 and entry-1 with:
    mask_tls_certificate: "{{ lookup('file', playbook_dir ~ '/../.local-secrets/vmshare.ru-fullchain.pem') }}"
    mask_tls_private_key: "{{ lookup('file', playbook_dir ~ '/../.local-secrets/vmshare.ru-privkey.pem') }}"

## 5. Issue
    make certs
Cert is issued on control-1, auto-renewed by certbot.timer, and fetched to
.local-secrets/vmshare.ru-{fullchain,privkey}.pem on your laptop.

## Renewal propagation
certbot.timer renews on control-1 automatically. To push a renewed cert to the
nodes, re-run:  make certs && make apply LIMIT=exit-1,entry-1
(Full hands-off propagation via Vault can be added later.)
