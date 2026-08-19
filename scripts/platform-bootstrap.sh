#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$repository_root"

usage() {
  cat <<'EOF'
Usage:
  scripts/platform-bootstrap.sh                        # validate only; no host changes
  scripts/platform-bootstrap.sh --apply                # validate, confirm, bootstrap, verify
  scripts/platform-bootstrap.sh --reuse-tunnel --apply # deliver bundle values to a hardened hub

Runs the complete operator-side platform bootstrap gate from one committed
checkout. Vault is installed and checked, but this script never changes its
initialization, seal, policy, authentication, or secret state.

--reuse-tunnel is for a hub that is already bootstrapped: its public SSH is
closed by design, so the clean-host phase cannot run and is replaced by the
management tunnel that phase created. This is how a new bundle variable reaches
a hardened hub without editing anything on it by hand.
EOF
}

apply=false
reuse_tunnel=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) apply=true ;;
    --reuse-tunnel) reuse_tunnel=true ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 64 ;;
  esac
  shift
done

required_commands=(
  git make python3 sops wg wg-quick ansible ansible-playbook yamllint ansible-lint
)
if [[ "$apply" == true || "$reuse_tunnel" == true ]]; then
  required_commands+=(sudo ip)
fi
for command_name in "${required_commands[@]}"; do
  command -v "$command_name" >/dev/null 2>&1 || {
    printf 'required command is unavailable: %s\n' "$command_name" >&2
    exit 69
  }
done

if ! git diff --quiet || ! git diff --cached --quiet || \
   [[ -n "$(git ls-files --others --exclude-standard)" ]]; then
  printf '%s\n' 'refusing bootstrap from a dirty working tree; commit or remove every change first' >&2
  git status --short >&2
  exit 65
fi

source_sha="$(git rev-parse --verify HEAD^{commit})"
branch="$(git symbolic-ref --quiet --short HEAD || printf '%s' detached)"
printf 'validated source: %s (%s)\n' "$source_sha" "$branch"

platform_bundle="${PLATFORM_BUNDLE:-inventories/bootstrap/platform.sops.yml}"
operator_key="${PLATFORM_WIREGUARD_PRIVATE_KEY:-$HOME/.config/spiritvpn/keys/operator-wg.key}"
[[ -f "$platform_bundle" ]] || {
  printf 'platform bundle is missing: %s\n' "$platform_bundle" >&2
  exit 66
}
[[ ! -L "$operator_key" && -f "$operator_key" ]] || {
  printf 'operator WireGuard private key is missing or unsafe: %s\n' "$operator_key" >&2
  exit 66
}

export ANSIBLE_LOCAL_TEMP="${ANSIBLE_LOCAL_TEMP:-/tmp/spiritvpn-ansible-local}"
mkdir -p -- "$ANSIBLE_LOCAL_TEMP"
chmod 0700 "$ANSIBLE_LOCAL_TEMP"

printf '%s\n' '1/4: running repository checks'
make check

printf '%s\n' '2/4: running YAML and Ansible lint'
make lint

printf '%s\n' '3/4: validating the encrypted platform bundle'
make fleet-platform-check PLATFORM_BUNDLE="$platform_bundle"

if [[ "$reuse_tunnel" == true ]]; then
  printf '%s\n' '4/4: checking the management tunnel; the hardened hub closes public SSH'
  ip link show dev spiritvpn-mgmt >/dev/null || {
    printf '%s\n' 'management tunnel spiritvpn-mgmt is not up; nothing to reuse' >&2
    exit 66
  }
else
  printf '%s\n' '4/4: checking pinned SSH connectivity without changing the host'
  make fleet-platform-bootstrap-check \
    CONNECT=1 \
    PLATFORM_BUNDLE="$platform_bundle"
fi

if [[ "$apply" != true ]]; then
  printf '%s\n' 'validation complete; no local or remote bootstrap changes were made'
  if [[ "$reuse_tunnel" == true ]]; then
    printf '%s\n' 'run scripts/platform-bootstrap.sh --reuse-tunnel --apply to deliver the bundle'
  else
    printf '%s\n' 'run scripts/platform-bootstrap.sh --apply to perform the bootstrap'
  fi
  exit 0
fi

[[ -t 0 && -t 1 ]] || {
  printf '%s\n' 'bootstrap apply requires an interactive terminal' >&2
  exit 67
}

if [[ "$reuse_tunnel" == true ]]; then
  target=fleet-platform-refresh
  cat <<EOF

The next step will reapply the encrypted platform bundle to the already
hardened management VPS through the existing tunnel, from commit $source_sha.
The local WireGuard interface is reused, not rewritten.
Type APPLY to continue.
EOF
else
  target=fleet-platform-bootstrap
  cat <<EOF

The next step will modify the management VPS and install the managed local
WireGuard interface from commit $source_sha.
Bootstrap will preserve the current Vault initialization and seal state.
Type APPLY to continue.
EOF
fi
read -r confirmation
[[ "$confirmation" == APPLY ]] || { printf '%s\n' 'bootstrap cancelled'; exit 68; }

sudo -v
make "$target" \
  APPLY=1 \
  PLATFORM_BUNDLE="$platform_bundle" \
  PLATFORM_WIREGUARD_PRIVATE_KEY="$operator_key"

sudo test -f /etc/wireguard/spiritvpn-mgmt.conf
ip link show dev spiritvpn-mgmt >/dev/null
sudo wg show spiritvpn-mgmt

if [[ "$reuse_tunnel" == true ]]; then
  cat <<'EOF'

The hub persisted the reviewed bundle as its runtime configuration.
Run the platform-deploy workflow to reconcile the steady state from it.
EOF
else
  cat <<'EOF'

Platform bootstrap and convergence verification completed.
Vault is reachable. Bootstrap preserved its initialization and seal state.
For a new Vault, perform the recovery ceremony only after external storage for
unseal shares and the initial root token is ready.
EOF
fi
