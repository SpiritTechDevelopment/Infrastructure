INVENTORY ?= inventories/prod/inventory.yml
STATIC_INVENTORY ?= examples/static-check-inventory.yml
TARGET = entry:exit
LIMIT ?=
NODE ?= entry-1
ENTRY ?= entry-1
EXIT ?=
ENDPOINT ?=
UUID ?=
EMAIL ?=
OUT ?=
PATTERN ?=
STATE ?=
PRUNE ?=
REPLACE ?=
EXTRA_VARS ?=
ASK_PASS ?= 0
SSH_AUTH ?= $(if $(filter 1 yes true,$(ASK_PASS)),password,auto)
SSH_KEY ?=
ANSIBLE_EXTRA_ARGS ?=

# SOPS-encrypted deploy secrets (committed) and their decrypted form (gitignored).
# `make decrypt` materializes the plaintext; deploy targets depend on it and pass
# it to Ansible as extra-vars. Replaces the old hand-placed .local-secrets files.
SECRETS_SOPS  ?= inventories/prod/secrets.sops.yml
SECRETS_PLAIN ?= inventories/prod/secrets.plain.yml
SECRETS_ARGS  = --extra-vars @$(SECRETS_PLAIN)

# SOPS-encrypted inventory (committed, whole-file/binary) and its decrypted form
# ($(INVENTORY), gitignored). `make decrypt` materializes it so a clean clone can
# reproduce the fleet topology. Edit the topology via `sops $(INVENTORY_SOPS)`.
INVENTORY_SOPS ?= inventories/prod/inventory.sops.yml

# SSH_AUTH modes:
#   auto      use normal OpenSSH config/agent/default keys
#   key       use exactly SSH_KEY and do not offer agent keys
#   password  prompt once for the SSH password and disable public-key attempts
SSH_AUTH_ARGS = $(if $(filter password,$(SSH_AUTH)),--ask-pass --ssh-common-args='-o PubkeyAuthentication=no -o PreferredAuthentications=password',$(if $(filter key,$(SSH_AUTH)),--private-key "$(SSH_KEY)" --ssh-common-args='-o IdentitiesOnly=yes -o PreferredAuthentications=publickey',$(if $(SSH_KEY),--private-key "$(SSH_KEY)",)))
PLAYBOOK = ansible-playbook $(SSH_AUTH_ARGS) $(ANSIBLE_EXTRA_ARGS)
ADHOC = ansible $(SSH_AUTH_ARGS) $(ANSIBLE_EXTRA_ARGS)

API_TARGET = $(if $(ENDPOINT),$(ENDPOINT),$(NODE))

help: ## Show targets
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-22s %s\n",$$1,$$2}'

deps: ## Check local controller prerequisites (no external collections required)
	@python3 -c 'import ansible,sys; parts=tuple(int(x) for x in ansible.__version__.split(".")[:2]); sys.exit("ansible-core >=2.18,<2.19 required; found " + ansible.__version__) if parts != (2,18) else print("ansible-core", ansible.__version__, "OK")'
	@for cmd in ansible-playbook ansible-inventory python3 curl openssl timeout; do command -v "$$cmd" >/dev/null || { echo "$$cmd is required" >&2; exit 2; }; done
	@command -v docker >/dev/null || command -v xray >/dev/null || (echo 'Docker or local Xray is required for E2E' >&2; exit 2)
	@if [ "$(SSH_AUTH)" = password ]; then command -v sshpass >/dev/null || { echo 'sshpass is required for SSH_AUTH=password (Ubuntu: sudo apt install sshpass)' >&2; exit 2; }; fi
	@if [ "$(SSH_AUTH)" = key ] && [ -z "$(SSH_KEY)" ]; then echo 'SSH_KEY is required for SSH_AUTH=key' >&2; exit 2; fi
	@if [ "$(SSH_AUTH)" = key ] && [ ! -r "$(SSH_KEY)" ]; then echo 'SSH_KEY is not readable: $(SSH_KEY)' >&2; exit 2; fi
	@case "$(SSH_AUTH)" in auto|key|password) ;; *) echo 'SSH_AUTH must be auto, key, or password' >&2; exit 2;; esac

inventory: ## Show parsed inventory graph
	ansible-inventory -i "$(INVENTORY)" --graph

ping: ## Test SSH and Python; LIMIT is optional
	$(ADHOC) -i "$(INVENTORY)" all -m ping $(if $(LIMIT),--limit "$(LIMIT)",)

lint: ## Run YAML and Ansible lint
	yamllint .
	ANSIBLE_INVENTORY=$(STATIC_INVENTORY) ansible-lint

syntax: ## Syntax-check the full site and standalone utility playbooks
	@for playbook in \
		playbooks/site.yml \
		playbooks/fleet-infra.yml \
		playbooks/backend-staging.yml \
		playbooks/telegram-bot-staging.yml \
		playbooks/acme.yml \
		playbooks/management-network.yml; do \
		ANSIBLE_INVENTORY="$(STATIC_INVENTORY)" ansible-playbook "$$playbook" --syntax-check || exit $$?; \
	done

render: ## Render and validate Xray configs
	ANSIBLE_INVENTORY="$(STATIC_INVENTORY)" ./scripts/render-check.sh $(TARGET)

check: ## Run all local static, parser, dashboard, syntax, and render checks
	@for script in scripts/*.sh; do bash -n "$$script" || exit $$?; done
	@python3 -m py_compile scripts/*.py
	@for dashboard in roles/observability/files/dashboards/*.json; do python3 -m json.tool "$$dashboard" >/dev/null || exit $$?; done
	ANSIBLE_INVENTORY="$(STATIC_INVENTORY)" ansible-playbook playbooks/reality-key-parser-test.yml
	$(MAKE) syntax STATIC_INVENTORY="$(STATIC_INVENTORY)"
	$(MAKE) render STATIC_INVENTORY="$(STATIC_INVENTORY)" TARGET="$(TARGET)"

test-api-wrapper: ## Offline test of add/list/stats/remove wrapper semantics
	./scripts/test-api-wrapper.sh

decrypt: ## Materialize SOPS-encrypted deploy secrets + inventory (gitignored)
	@if [ -f "$(SECRETS_SOPS)" ]; then \
	  command -v sops >/dev/null || { echo "sops is required (see OPERATIONS.md)" >&2; exit 2; }; \
	  sops -d "$(SECRETS_SOPS)" > "$(SECRETS_PLAIN)" && chmod 600 "$(SECRETS_PLAIN)" && \
	    echo "decrypted -> $(SECRETS_PLAIN)"; \
	else echo "no $(SECRETS_SOPS); nothing to decrypt"; fi
	@if [ -f "$(INVENTORY_SOPS)" ]; then \
	  command -v sops >/dev/null || { echo "sops is required (see OPERATIONS.md)" >&2; exit 2; }; \
	  sops -d --input-type yaml --output-type binary "$(INVENTORY_SOPS)" > "$(INVENTORY).tmp"; \
	  if [ -f "$(INVENTORY)" ] && ! cmp -s "$(INVENTORY).tmp" "$(INVENTORY)"; then \
	    cp -a "$(INVENTORY)" "$(INVENTORY).bak.$$(date +%Y%m%d-%H%M%S)"; \
	    echo "note: local $(INVENTORY) differed from $(INVENTORY_SOPS); backed it up before overwriting (edit via 'sops $(INVENTORY_SOPS)')"; \
	  fi; \
	  mv "$(INVENTORY).tmp" "$(INVENTORY)" && echo "materialized -> $(INVENTORY)"; \
	fi

deploy: decrypt ## One-command full deployment plus infrastructure/telemetry verification
	$(PLAYBOOK) -i "$(INVENTORY)" playbooks/site.yml $(SECRETS_ARGS)

verify: ## Re-run runtime, API, dashboards, logs, and metrics verification
	$(PLAYBOOK) -i "$(INVENTORY)" playbooks/verify.yml

e2e: ## Backend contract E2E through default or selected exit
	./scripts/smoke-backend.sh --inventory "$(INVENTORY)" --entry "$(ENTRY)" $(if $(EXIT),--exit "$(EXIT)",)

e2e-all: ## Run API/customer E2E through every enabled exit
	./scripts/smoke-all-exits.sh --inventory "$(INVENTORY)" --entry "$(ENTRY)"

deploy-e2e: ## Static checks, full deployment, infrastructure verification, and backend/customer E2E
	$(MAKE) deps
	$(MAKE) check
	$(MAKE) deploy INVENTORY="$(INVENTORY)"
	$(if $(EXIT),$(MAKE) e2e INVENTORY="$(INVENTORY)" ENTRY="$(ENTRY)" EXIT="$(EXIT)",$(MAKE) e2e-all INVENTORY="$(INVENTORY)" ENTRY="$(ENTRY)")

platform: decrypt ## Deploy only Vault and observability
	$(PLAYBOOK) -i "$(INVENTORY)" playbooks/platform.yml $(SECRETS_ARGS) $(if $(LIMIT),--limit "$(LIMIT)",)

backend-staging: decrypt ## Deploy the immutable SpiritVPN backend image on control-1
	$(PLAYBOOK) -i "$(INVENTORY)" playbooks/backend-staging.yml $(SECRETS_ARGS) $(if $(EXTRA_VARS),--extra-vars "@$(EXTRA_VARS)",)

telegram-bot-staging: decrypt ## Deploy the immutable SpiritVPN Telegram bot image on control-1
	$(PLAYBOOK) -i "$(INVENTORY)" playbooks/telegram-bot-staging.yml $(SECRETS_ARGS) $(if $(EXTRA_VARS),--extra-vars "@$(EXTRA_VARS)",)

wire: ## Rebuild entry outbounds from deployed exit REALITY client passwords
	$(PLAYBOOK) -i "$(INVENTORY)" playbooks/wire-fleet.yml

apply-node: decrypt ## Manual selected-node redeploy; LIMIT is mandatory and entries must be wired
	@test -n "$(LIMIT)" || (echo 'LIMIT is required' >&2; exit 2)
	$(PLAYBOOK) -i "$(INVENTORY)" playbooks/fleet-infra.yml $(SECRETS_ARGS) --limit "localhost,$(LIMIT)"

check-node: decrypt ## Dry-run selected-node redeploy; LIMIT is mandatory
	@test -n "$(LIMIT)" || (echo 'LIMIT is required' >&2; exit 2)
	$(PLAYBOOK) -i "$(INVENTORY)" playbooks/fleet-infra.yml $(SECRETS_ARGS) --check --diff --limit "localhost,$(LIMIT)"

api-ping: ## Test Xray API; NODE=entry-1 or ENDPOINT=host:10085
	XRAY_INVENTORY="$(INVENTORY)" ./scripts/xray-api.sh "$(API_TARGET)" ping

api-list: ## List runtime Xray users
	XRAY_INVENTORY="$(INVENTORY)" ./scripts/xray-api.sh "$(API_TARGET)" list


api-emails: ## List exact runtime user identifiers, one per line
	XRAY_INVENTORY="$(INVENTORY)" ./scripts/xray-api.sh "$(API_TARGET)" emails

api-has: ## Check exact runtime user presence; EMAIL=... [NODE=entry-1]
	@test -n "$(EMAIL)" || (echo 'EMAIL is required' >&2; exit 2)
	XRAY_INVENTORY="$(INVENTORY)" ./scripts/xray-api.sh "$(API_TARGET)" has "$(EMAIL)"

api-add: ## Add runtime user; UUID=... EMAIL=... [NODE=entry-1]
	@test -n "$(UUID)" || (echo 'UUID is required' >&2; exit 2)
	@test -n "$(EMAIL)" || (echo 'EMAIL is required' >&2; exit 2)
	XRAY_INVENTORY="$(INVENTORY)" ./scripts/xray-api.sh "$(API_TARGET)" add "$(UUID)" "$(EMAIL)"

api-remove: ## Remove runtime user; EMAIL=... [NODE=entry-1]
	@test -n "$(EMAIL)" || (echo 'EMAIL is required' >&2; exit 2)
	XRAY_INVENTORY="$(INVENTORY)" ./scripts/xray-api.sh "$(API_TARGET)" remove "$(EMAIL)"

api-stats: ## Show Xray stats; optional PATTERN=email
	XRAY_INVENTORY="$(INVENTORY)" ./scripts/xray-api.sh "$(API_TARGET)" stats $(PATTERN)

gen-client: ## Generate profile for an API user; UUID=... EMAIL=... [NODE=entry-1] [OUT=client.json]
	@test -n "$(UUID)" || (echo 'UUID is required' >&2; exit 2)
	@test -n "$(EMAIL)" || (echo 'EMAIL is required' >&2; exit 2)
	./scripts/gen-client.sh --node "$(NODE)" --inventory "$(INVENTORY)" --uuid "$(UUID)" --email "$(EMAIL)" --api "$(API_TARGET)" $(if $(OUT),--out "$(OUT)",)

smoke-via: ## Force a specific exit using operator selector; ENTRY=... EXIT=exit-fr
	@test -n "$(EXIT)" || (echo 'EXIT is required (e.g. exit-fr)' >&2; exit 2)
	./scripts/smoke-via.sh --inventory "$(INVENTORY)" --entry "$(ENTRY)" --exit "$(EXIT)"

reconcile: ## Replay desired users; STATE=path [PRUNE=1] [REPLACE=1]
	@test -n "$(STATE)" || (echo 'STATE is required' >&2; exit 2)
	XRAY_INVENTORY="$(INVENTORY)" ./scripts/xray-reconcile.sh "$(API_TARGET)" "$(STATE)" $(if $(filter 1 yes true,$(PRUNE)),--prune,) $(if $(filter 1 yes true,$(REPLACE)),--replace-existing,)

management: decrypt ## Deploy/reconcile the WireGuard management overlay (all active hosts; no --limit)
	$(PLAYBOOK) -i "$(INVENTORY)" playbooks/management-network.yml $(SECRETS_ARGS)

certs: ## Obtain/renew certificates; LIMIT defaults to control-1
	$(PLAYBOOK) -i "$(INVENTORY)" playbooks/acme.yml --limit "$(or $(LIMIT),control-1)" $(if $(EXTRA_VARS),--extra-vars "@$(EXTRA_VARS)",)

dns: decrypt ## Reconcile Cloudflare DNS from the inventory (plan; APPLY=1 to apply)
	$(PLAYBOOK) -i "$(INVENTORY)" playbooks/dns.yml $(SECRETS_ARGS) $(if $(filter 1 yes true,$(APPLY)),-e cloudflare_apply=true,)

.PHONY: help deps inventory ping lint syntax render check test-api-wrapper decrypt deploy verify e2e e2e-all deploy-e2e \
	backend-staging telegram-bot-staging \
	platform wire apply-node check-node api-ping api-list api-emails api-has api-add api-remove api-stats \
	gen-client smoke-via reconcile management certs dns
