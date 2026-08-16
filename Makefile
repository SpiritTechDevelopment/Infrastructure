SOURCE ?= HEAD
INITIAL ?= 0
CONNECT ?= 0
APPLY ?= 0
COMPILED_SECRETS ?=
BOOTSTRAP_VARS ?=
READINESS_VARS ?=
FLEET_STATE_DIR ?=
PLATFORM_BUNDLE ?= inventories/bootstrap/platform.sops.yml
PLATFORM_WIREGUARD_PRIVATE_KEY ?= $(HOME)/.config/spiritvpn/keys/operator-wg.key
RESUME ?= 0
REVISION ?=
ALLOW_DESTRUCTIVE ?= 0

help: ## Show targets
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-22s %s\n",$$1,$$2}'

fleet-validate: ## Validate desired state for all environments without network access
	@for environment in develop prod; do python3 -m fleetctl.cli validate --environment "$$environment" || exit $$?; done

fleet-test: ## Run offline fleetctl unit tests
	python3 -m unittest discover -s tests/unit -v

fleet-render: ## Render local artifacts; ENVIRONMENT=develop by default
	python3 -m fleetctl.cli render --environment "$(or $(ENVIRONMENT),develop)" --output "build/$(or $(ENVIRONMENT),develop)"

fleet-plan: ## Plan SOURCE=HEAD against deployment ref; INITIAL=1 only for first deploy
	python3 -m fleetctl.cli plan --environment "$(or $(ENVIRONMENT),develop)" --source "$(SOURCE)" $(if $(BASELINE),--baseline "$(BASELINE)",$(if $(filter 1 yes true,$(INITIAL)),--initial,)) --output "build/$(or $(ENVIRONMENT),develop)"

fleet-manifest: fleet-render ## Render backend manifest offline; requires explicit REVISION
	@test -n "$(REVISION)" || (echo 'REVISION is required' >&2; exit 2)
	python3 -m fleetctl.cli manifest --environment "$(or $(ENVIRONMENT),develop)" --source "$(SOURCE)" --revision "$(REVISION)" $(if $(BASELINE),--baseline "$(BASELINE)",$(if $(filter 1 yes true,$(INITIAL)),--initial,)) $(if $(filter 1 yes true,$(ALLOW_DESTRUCTIVE)),--allow-destructive,) --output "build/$(or $(ENVIRONMENT),develop)"

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

fleet-bootstrap-check: fleet-ansible-check ## CONNECT=1 checks bootstrap syntax and SSH connectivity
	@if [ "$(CONNECT)" = 1 ]; then \
	  command -v ansible-playbook >/dev/null && command -v ansible >/dev/null || { echo 'ansible and ansible-playbook are required for CONNECT=1' >&2; exit 2; }; \
	  ansible-playbook -i "build/$(or $(ENVIRONMENT),develop)/bootstrap-inventory.json" playbooks/bootstrap/bootstrap.yml --syntax-check $(if $(BOOTSTRAP_VARS),--extra-vars "@$(BOOTSTRAP_VARS)",) && \
	  ansible -i "build/$(or $(ENVIRONMENT),develop)/bootstrap-inventory.json" spiritvpn_bootstrap --module-name ping --one-line; \
	else echo 'local bootstrap artifacts valid; no SSH attempted (set CONNECT=1 explicitly)'; fi

fleet-bootstrap: fleet-ansible-check ## Bootstrap clean hosts; requires APPLY=1 and BOOTSTRAP_VARS=file
	@test "$(APPLY)" = 1 || (echo 'refusing bootstrap SSH/mutation: set APPLY=1 explicitly' >&2; exit 2)
	@test -n "$(BOOTSTRAP_VARS)" || (echo 'BOOTSTRAP_VARS is required' >&2; exit 2)
	ansible-playbook -i "build/$(or $(ENVIRONMENT),develop)/bootstrap-inventory.json" playbooks/bootstrap/bootstrap.yml --extra-vars "@$(BOOTSTRAP_VARS)"

fleet-deploy: ## Infrastructure coordinator; dry-run by default, APPLY=1 enables SSH
	python3 -m fleetctl.cli deploy --environment "$(or $(ENVIRONMENT),develop)" --source "$(SOURCE)" $(if $(filter 1 yes true,$(INITIAL)),--initial,) $(if $(filter 1 yes true,$(APPLY)),--apply,) $(if $(filter 1 yes true,$(RESUME)),--resume,) $(if $(filter 1 yes true,$(ALLOW_DESTRUCTIVE)),--allow-destructive,) $(if $(FLEET_STATE_DIR),--state-dir "$(FLEET_STATE_DIR)",) $(if $(BOOTSTRAP_VARS),--bootstrap-vars "$(BOOTSTRAP_VARS)",) $(if $(COMPILED_SECRETS),--compiled-secrets "$(COMPILED_SECRETS)",) $(if $(READINESS_VARS),--readiness-vars "$(READINESS_VARS)",)

fleet-platform-check: ## Validate the minimal bootstrap inventory; never connects
	python3 scripts/platform-sops.py check --bundle "$(PLATFORM_BUNDLE)"

fleet-platform-bootstrap-check: fleet-platform-check ## CONNECT=1 checks syntax and pinned SSH connectivity
	@if [ "$(CONNECT)" = 1 ]; then \
	  python3 scripts/platform-sops.py bootstrap-check --bundle "$(PLATFORM_BUNDLE)"; \
	else echo 'local platform artifacts valid; no SSH or Vault access attempted (set CONNECT=1 explicitly)'; fi

fleet-platform-bootstrap: fleet-platform-check ## Reconcile Vault and management foundation from SOPS; requires APPLY=1
	@test "$(APPLY)" = 1 || (echo 'refusing platform SSH/mutation: set APPLY=1 explicitly' >&2; exit 2)
	python3 scripts/bootstrap-platform.py --apply --verify-convergence \
	  --bundle "$(PLATFORM_BUNDLE)" \
	  --operator-wireguard-private-key "$(PLATFORM_WIREGUARD_PRIVATE_KEY)"

syntax: ## Syntax-check the active v1 playbooks
	@for playbook in \
		playbooks/bootstrap/bootstrap.yml \
		playbooks/control/deploy.yml \
		playbooks/deploy/configure.yml \
		playbooks/operations/readiness.yml \
		playbooks/platform/bootstrap.yml \
		playbooks/platform/wireguard-bootstrap.yml \
		playbooks/platform/steady.yml \
		playbooks/platform/readiness.yml; do \
		ansible-playbook -i tests/fixtures/platform-bootstrap/platform.yml "$$playbook" --syntax-check || exit $$?; \
	done

lint: ## Run YAML and Ansible lint on the active v1 contour
	@git ls-files -- '*.yml' '*.yaml' | while IFS= read -r file; do \
	  test ! -f "$$file" || printf '%s\0' "$$file"; \
	done | xargs -0 --no-run-if-empty yamllint
	ANSIBLE_INVENTORY="$(CURDIR)/tests/fixtures/platform-bootstrap/platform.yml" \
		ansible-lint playbooks roles

check: fleet-validate fleet-test ## Run local v1 static checks
	@for script in scripts/*.sh; do bash -n "$$script" || exit $$?; done
	@python3 -m py_compile scripts/*.py
	@if command -v ansible-playbook >/dev/null; then $(MAKE) syntax; else echo 'ansible-playbook unavailable; syntax check skipped' >&2; fi

.PHONY: help fleet-validate fleet-test fleet-render fleet-plan fleet-manifest \
	fleet-ansible-check fleet-configure-check fleet-configure fleet-provisioning-check \
	fleet-bootstrap-check fleet-bootstrap fleet-deploy fleet-platform-check \
	fleet-platform-bootstrap-check fleet-platform-bootstrap syntax lint check
