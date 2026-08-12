# Legacy contour only. fleet-* targets use generated build/<env> inventories.
LEGACY_INVENTORY ?= inventories/prod/inventory.yml
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
SOURCE ?= HEAD
INITIAL ?= 0
CONNECT ?= 0
APPLY ?= 0
ALLOW_LEGACY ?= 0
COMPILED_SECRETS ?=
BOOTSTRAP_VARS ?=
READINESS_VARS ?=
RESUME ?= 0

# SOPS-encrypted deploy secrets (committed) and their decrypted form (gitignored).
# `make legacy-decrypt ALLOW_LEGACY=1` materializes the plaintext; legacy deploy
# targets depend on it and pass it to Ansible as extra-vars.
SECRETS_SOPS  ?= inventories/prod/secrets.sops.yml
SECRETS_PLAIN ?= inventories/prod/secrets.plain.yml
SECRETS_ARGS  = --extra-vars @$(SECRETS_PLAIN)

# SOPS-encrypted inventory (committed, whole-file/binary) and its decrypted form
# ($(LEGACY_INVENTORY), gitignored). The guarded legacy decrypt target
# materializes it. Edit the old topology via `sops $(INVENTORY_SOPS)`.
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

fleet-validate: ## Validate desired state for all environments without network access
	@for environment in develop staging prod; do python3 -m fleetctl.cli validate --environment "$$environment" || exit $$?; done

fleet-test: ## Run offline fleetctl unit tests
	python3 -m unittest discover -s tests/unit -v

fleet-render: ## Render local artifacts; ENVIRONMENT=develop by default
	python3 -m fleetctl.cli render --environment "$(or $(ENVIRONMENT),develop)" --output "build/$(or $(ENVIRONMENT),develop)"

fleet-plan: ## Plan SOURCE=HEAD against deployment ref; INITIAL=1 only for first deploy
	python3 -m fleetctl.cli plan --environment "$(or $(ENVIRONMENT),develop)" --source "$(SOURCE)" $(if $(BASELINE),--baseline "$(BASELINE)",$(if $(filter 1 yes true,$(INITIAL)),--initial,)) --output "build/$(or $(ENVIRONMENT),develop)"

fleet-ansible-check: ## Validate compiled inventory/node plans locally; never connects
	python3 -m fleetctl.cli ansible-check --environment "$(or $(ENVIRONMENT),develop)" --build-dir "build/$(or $(ENVIRONMENT),develop)"
	@if command -v ansible-inventory >/dev/null; then \
	  ansible-inventory -i "build/$(or $(ENVIRONMENT),develop)/ansible-inventory.json" --list >/dev/null && \
	  ansible-inventory -i "build/$(or $(ENVIRONMENT),develop)/bootstrap-inventory.json" --list >/dev/null; \
	else echo 'ansible-inventory unavailable; parser check skipped' >&2; fi

fleet-configure-check: fleet-ansible-check ## Local check by default; CONNECT=1 enables SSH --check
	@if [ "$(CONNECT)" = 1 ]; then \
	  command -v ansible-playbook >/dev/null || { echo 'ansible-playbook is required for CONNECT=1' >&2; exit 2; }; \
	  ansible-playbook -i "build/$(or $(ENVIRONMENT),develop)/ansible-inventory.json" playbooks/deploy/configure.yml --check --diff $(if $(COMPILED_SECRETS),--extra-vars "@$(COMPILED_SECRETS)",); \
	else echo 'local compiled-artifact checks complete; no SSH attempted (set CONNECT=1 explicitly)'; fi

fleet-configure: fleet-ansible-check ## Apply compiled configure; requires APPLY=1 and COMPILED_SECRETS=file
	@test "$(APPLY)" = 1 || (echo 'refusing SSH/mutation: set APPLY=1 explicitly' >&2; exit 2)
	@test -n "$(COMPILED_SECRETS)" || (echo 'COMPILED_SECRETS is required for protected secret resolution' >&2; exit 2)
	ansible-playbook -i "build/$(or $(ENVIRONMENT),develop)/ansible-inventory.json" playbooks/deploy/configure.yml --extra-vars "@$(COMPILED_SECRETS)"

fleet-provisioning-check: ## Validate manual VPS declarations; never calls provider APIs
	python3 -m fleetctl.cli provisioning-check --environment "$(or $(ENVIRONMENT),develop)"

fleet-bootstrap-check: fleet-ansible-check ## Local check by default; CONNECT=1 enables bootstrap SSH --check
	@if [ "$(CONNECT)" = 1 ]; then \
	  command -v ansible-playbook >/dev/null || { echo 'ansible-playbook is required for CONNECT=1' >&2; exit 2; }; \
	  ansible-playbook -i "build/$(or $(ENVIRONMENT),develop)/bootstrap-inventory.json" playbooks/bootstrap/bootstrap.yml --check --diff $(if $(BOOTSTRAP_VARS),--extra-vars "@$(BOOTSTRAP_VARS)",); \
	else echo 'local bootstrap artifacts valid; no SSH attempted (set CONNECT=1 explicitly)'; fi

fleet-bootstrap: fleet-ansible-check ## Bootstrap clean hosts; requires APPLY=1 and BOOTSTRAP_VARS=file
	@test "$(APPLY)" = 1 || (echo 'refusing bootstrap SSH/mutation: set APPLY=1 explicitly' >&2; exit 2)
	@test -n "$(BOOTSTRAP_VARS)" || (echo 'BOOTSTRAP_VARS is required' >&2; exit 2)
	ansible-playbook -i "build/$(or $(ENVIRONMENT),develop)/bootstrap-inventory.json" playbooks/bootstrap/bootstrap.yml --extra-vars "@$(BOOTSTRAP_VARS)"

fleet-deploy: ## Infrastructure coordinator; dry-run by default, APPLY=1 enables SSH
	python3 -m fleetctl.cli deploy --environment "$(or $(ENVIRONMENT),develop)" --source "$(SOURCE)" $(if $(filter 1 yes true,$(INITIAL)),--initial,) $(if $(filter 1 yes true,$(APPLY)),--apply,) $(if $(filter 1 yes true,$(RESUME)),--resume,) $(if $(BOOTSTRAP_VARS),--bootstrap-vars "$(BOOTSTRAP_VARS)",) $(if $(COMPILED_SECRETS),--compiled-secrets "$(COMPILED_SECRETS)",) $(if $(READINESS_VARS),--readiness-vars "$(READINESS_VARS)",)

legacy-guard:
	@test "$(ALLOW_LEGACY)" = 1 || { \
	  echo 'legacy production operations are disabled; use fleet-* targets (ALLOW_LEGACY=1 is an explicit break-glass override)' >&2; \
	  exit 2; \
	}

legacy-deps: legacy-guard ## [LEGACY] Check old controller prerequisites; requires ALLOW_LEGACY=1
	@python3 -c 'import ansible,sys; parts=tuple(int(x) for x in ansible.__version__.split(".")[:2]); sys.exit("ansible-core >=2.18,<2.19 required; found " + ansible.__version__) if parts != (2,18) else print("ansible-core", ansible.__version__, "OK")'
	@for cmd in ansible-playbook ansible-inventory python3 curl openssl timeout; do command -v "$$cmd" >/dev/null || { echo "$$cmd is required" >&2; exit 2; }; done
	@command -v docker >/dev/null || command -v xray >/dev/null || (echo 'Docker or local Xray is required for E2E' >&2; exit 2)
	@if [ "$(SSH_AUTH)" = password ]; then command -v sshpass >/dev/null || { echo 'sshpass is required for SSH_AUTH=password (Ubuntu: sudo apt install sshpass)' >&2; exit 2; }; fi
	@if [ "$(SSH_AUTH)" = key ] && [ -z "$(SSH_KEY)" ]; then echo 'SSH_KEY is required for SSH_AUTH=key' >&2; exit 2; fi
	@if [ "$(SSH_AUTH)" = key ] && [ ! -r "$(SSH_KEY)" ]; then echo 'SSH_KEY is not readable: $(SSH_KEY)' >&2; exit 2; fi
	@case "$(SSH_AUTH)" in auto|key|password) ;; *) echo 'SSH_AUTH must be auto, key, or password' >&2; exit 2;; esac

legacy-inventory: legacy-guard ## [LEGACY] Show the manual inventory graph; requires ALLOW_LEGACY=1
	ansible-inventory -i "$(LEGACY_INVENTORY)" --graph

legacy-ping: legacy-guard ## [LEGACY] Test SSH and Python; requires ALLOW_LEGACY=1
	$(ADHOC) -i "$(LEGACY_INVENTORY)" all -m ping $(if $(LIMIT),--limit "$(LIMIT)",)

lint: ## Run YAML and Ansible lint
	yamllint .
	ANSIBLE_INVENTORY=$(STATIC_INVENTORY) ansible-lint

syntax: ## Syntax-check the full site and standalone utility playbooks
	@for playbook in \
		playbooks/site.yml \
		playbooks/fleet-infra.yml \
		playbooks/backend-staging.yml \
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
	ANSIBLE_INVENTORY="$(STATIC_INVENTORY)" ansible-playbook playbooks/tests/reality-key-parser-test.yml
	$(MAKE) syntax STATIC_INVENTORY="$(STATIC_INVENTORY)"
	$(MAKE) render STATIC_INVENTORY="$(STATIC_INVENTORY)" TARGET="$(TARGET)"

test-api-wrapper: ## Offline test of add/list/stats/remove wrapper semantics
	./scripts/test-api-wrapper.sh

legacy-decrypt: legacy-guard ## [LEGACY] Materialize old SOPS secrets/inventory; requires ALLOW_LEGACY=1
	@if [ -f "$(SECRETS_SOPS)" ]; then \
	  command -v sops >/dev/null || { echo "sops is required (see OPERATIONS.md)" >&2; exit 2; }; \
	  sops -d "$(SECRETS_SOPS)" > "$(SECRETS_PLAIN)" && chmod 600 "$(SECRETS_PLAIN)" && \
	    echo "decrypted -> $(SECRETS_PLAIN)"; \
	else echo "no $(SECRETS_SOPS); nothing to decrypt"; fi
	@if [ -f "$(INVENTORY_SOPS)" ]; then \
	  command -v sops >/dev/null || { echo "sops is required (see OPERATIONS.md)" >&2; exit 2; }; \
	  sops -d --input-type yaml --output-type binary "$(INVENTORY_SOPS)" > "$(LEGACY_INVENTORY).tmp"; \
	  if [ -f "$(LEGACY_INVENTORY)" ] && ! cmp -s "$(LEGACY_INVENTORY).tmp" "$(LEGACY_INVENTORY)"; then \
	    cp -a "$(LEGACY_INVENTORY)" "$(LEGACY_INVENTORY).bak.$$(date +%Y%m%d-%H%M%S)"; \
	    echo "note: local $(LEGACY_INVENTORY) differed from $(INVENTORY_SOPS); backed it up before overwriting (edit via 'sops $(INVENTORY_SOPS)')"; \
	  fi; \
	  mv "$(LEGACY_INVENTORY).tmp" "$(LEGACY_INVENTORY)" && echo "materialized -> $(LEGACY_INVENTORY)"; \
	fi

legacy-deploy: legacy-decrypt ## [LEGACY] Old full deployment; requires ALLOW_LEGACY=1
	$(PLAYBOOK) -i "$(LEGACY_INVENTORY)" playbooks/site.yml $(SECRETS_ARGS)

legacy-verify: legacy-guard ## [LEGACY] Old live verification; requires ALLOW_LEGACY=1
	$(PLAYBOOK) -i "$(LEGACY_INVENTORY)" playbooks/verify.yml

legacy-e2e: legacy-guard ## [LEGACY] Old backend E2E; requires ALLOW_LEGACY=1
	./scripts/smoke-backend.sh --inventory "$(LEGACY_INVENTORY)" --entry "$(ENTRY)" $(if $(EXIT),--exit "$(EXIT)",)

legacy-e2e-all: legacy-guard ## [LEGACY] Old all-exit E2E; requires ALLOW_LEGACY=1
	./scripts/smoke-all-exits.sh --inventory "$(LEGACY_INVENTORY)" --entry "$(ENTRY)"

legacy-deploy-e2e: legacy-guard ## [LEGACY] Old deploy and E2E; requires ALLOW_LEGACY=1
	$(MAKE) legacy-deps
	$(MAKE) check
	$(MAKE) legacy-deploy LEGACY_INVENTORY="$(LEGACY_INVENTORY)"
	$(if $(EXIT),$(MAKE) legacy-e2e LEGACY_INVENTORY="$(LEGACY_INVENTORY)" ENTRY="$(ENTRY)" EXIT="$(EXIT)",$(MAKE) legacy-e2e-all LEGACY_INVENTORY="$(LEGACY_INVENTORY)" ENTRY="$(ENTRY)")

legacy-platform: legacy-decrypt ## [LEGACY] Deploy old Vault/observability; requires ALLOW_LEGACY=1
	$(PLAYBOOK) -i "$(LEGACY_INVENTORY)" playbooks/platform.yml $(SECRETS_ARGS) $(if $(LIMIT),--limit "$(LIMIT)",)

legacy-backend-staging: legacy-decrypt ## [LEGACY] Deploy old backend; requires ALLOW_LEGACY=1
	$(PLAYBOOK) -i "$(LEGACY_INVENTORY)" playbooks/backend-staging.yml $(SECRETS_ARGS) $(if $(EXTRA_VARS),--extra-vars "@$(EXTRA_VARS)",)

legacy-wire: legacy-guard ## [LEGACY] Rebuild old entry outbounds; requires ALLOW_LEGACY=1
	$(PLAYBOOK) -i "$(LEGACY_INVENTORY)" playbooks/wire-fleet.yml

legacy-apply-node: legacy-decrypt ## [LEGACY] Redeploy an old selected node; requires ALLOW_LEGACY=1
	@test -n "$(LIMIT)" || (echo 'LIMIT is required' >&2; exit 2)
	$(PLAYBOOK) -i "$(LEGACY_INVENTORY)" playbooks/fleet-infra.yml $(SECRETS_ARGS) --limit "localhost,$(LIMIT)"

legacy-check-node: legacy-decrypt ## [LEGACY] Check an old selected node; requires ALLOW_LEGACY=1
	@test -n "$(LIMIT)" || (echo 'LIMIT is required' >&2; exit 2)
	$(PLAYBOOK) -i "$(LEGACY_INVENTORY)" playbooks/fleet-infra.yml $(SECRETS_ARGS) --check --diff --limit "localhost,$(LIMIT)"

legacy-api-ping: legacy-guard ## [LEGACY] Test old Xray API; requires ALLOW_LEGACY=1
	XRAY_INVENTORY="$(LEGACY_INVENTORY)" ./scripts/xray-api.sh "$(API_TARGET)" ping

legacy-api-list: legacy-guard ## [LEGACY] List old runtime users; requires ALLOW_LEGACY=1
	XRAY_INVENTORY="$(LEGACY_INVENTORY)" ./scripts/xray-api.sh "$(API_TARGET)" list


legacy-api-emails: legacy-guard ## [LEGACY] List old runtime identifiers; requires ALLOW_LEGACY=1
	XRAY_INVENTORY="$(LEGACY_INVENTORY)" ./scripts/xray-api.sh "$(API_TARGET)" emails

legacy-api-has: legacy-guard ## [LEGACY] Check an old runtime user; requires ALLOW_LEGACY=1
	@test -n "$(EMAIL)" || (echo 'EMAIL is required' >&2; exit 2)
	XRAY_INVENTORY="$(LEGACY_INVENTORY)" ./scripts/xray-api.sh "$(API_TARGET)" has "$(EMAIL)"

legacy-api-add: legacy-guard ## [LEGACY] Add an old runtime user; requires ALLOW_LEGACY=1
	@test -n "$(UUID)" || (echo 'UUID is required' >&2; exit 2)
	@test -n "$(EMAIL)" || (echo 'EMAIL is required' >&2; exit 2)
	XRAY_INVENTORY="$(LEGACY_INVENTORY)" ./scripts/xray-api.sh "$(API_TARGET)" add "$(UUID)" "$(EMAIL)"

legacy-api-remove: legacy-guard ## [LEGACY] Remove an old runtime user; requires ALLOW_LEGACY=1
	@test -n "$(EMAIL)" || (echo 'EMAIL is required' >&2; exit 2)
	XRAY_INVENTORY="$(LEGACY_INVENTORY)" ./scripts/xray-api.sh "$(API_TARGET)" remove "$(EMAIL)"

legacy-api-stats: legacy-guard ## [LEGACY] Show old Xray stats; requires ALLOW_LEGACY=1
	XRAY_INVENTORY="$(LEGACY_INVENTORY)" ./scripts/xray-api.sh "$(API_TARGET)" stats $(PATTERN)

legacy-gen-client: legacy-guard ## [LEGACY] Generate an old API profile; requires ALLOW_LEGACY=1
	@test -n "$(UUID)" || (echo 'UUID is required' >&2; exit 2)
	@test -n "$(EMAIL)" || (echo 'EMAIL is required' >&2; exit 2)
	./scripts/gen-client.sh --node "$(NODE)" --inventory "$(LEGACY_INVENTORY)" --uuid "$(UUID)" --email "$(EMAIL)" --api "$(API_TARGET)" $(if $(OUT),--out "$(OUT)",)

legacy-smoke-via: legacy-guard ## [LEGACY] Smoke-test an old exit; requires ALLOW_LEGACY=1
	@test -n "$(EXIT)" || (echo 'EXIT is required (e.g. exit-fr)' >&2; exit 2)
	./scripts/smoke-via.sh --inventory "$(LEGACY_INVENTORY)" --entry "$(ENTRY)" --exit "$(EXIT)"

legacy-reconcile: legacy-guard ## [LEGACY] Replay old desired users; requires ALLOW_LEGACY=1
	@test -n "$(STATE)" || (echo 'STATE is required' >&2; exit 2)
	XRAY_INVENTORY="$(LEGACY_INVENTORY)" ./scripts/xray-reconcile.sh "$(API_TARGET)" "$(STATE)" $(if $(filter 1 yes true,$(PRUNE)),--prune,) $(if $(filter 1 yes true,$(REPLACE)),--replace-existing,)

legacy-management: legacy-decrypt ## [LEGACY] Deploy old management overlay; requires ALLOW_LEGACY=1
	$(PLAYBOOK) -i "$(LEGACY_INVENTORY)" playbooks/management-network.yml $(SECRETS_ARGS)

legacy-certs: legacy-guard ## [LEGACY] Obtain old certificates; requires ALLOW_LEGACY=1
	$(PLAYBOOK) -i "$(LEGACY_INVENTORY)" playbooks/acme.yml --limit "$(or $(LIMIT),control-1)" $(if $(EXTRA_VARS),--extra-vars "@$(EXTRA_VARS)",)

legacy-dns: legacy-decrypt ## [LEGACY] Reconcile old Cloudflare DNS; requires ALLOW_LEGACY=1
	$(PLAYBOOK) -i "$(LEGACY_INVENTORY)" playbooks/dns.yml $(SECRETS_ARGS) $(if $(filter 1 yes true,$(APPLY)),-e cloudflare_apply=true,)

.PHONY: help fleet-validate fleet-test fleet-render fleet-plan fleet-ansible-check fleet-configure-check fleet-configure fleet-provisioning-check fleet-bootstrap-check fleet-bootstrap fleet-deploy lint syntax render check test-api-wrapper \
	legacy-guard legacy-deps legacy-inventory legacy-ping legacy-decrypt legacy-deploy legacy-verify legacy-e2e legacy-e2e-all legacy-deploy-e2e \
	legacy-backend-staging legacy-platform legacy-wire legacy-apply-node legacy-check-node legacy-api-ping legacy-api-list legacy-api-emails legacy-api-has legacy-api-add legacy-api-remove legacy-api-stats \
	legacy-gen-client legacy-smoke-via legacy-reconcile legacy-management legacy-certs legacy-dns
