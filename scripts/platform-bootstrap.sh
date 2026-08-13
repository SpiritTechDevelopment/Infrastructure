#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$repository_root"

usage() {
  cat <<'EOF'
Usage:
  scripts/platform-bootstrap.sh           # validate only; no host changes
  scripts/platform-bootstrap.sh --apply   # validate, confirm, bootstrap, verify

Runs the complete operator-side platform bootstrap gate from one committed
checkout. Vault is installed and checked but is deliberately not initialized,
unsealed, configured, or populated by this script.
EOF
}

apply=false
case "${1:-}" in
  "") ;;
  --apply) apply=true ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 64 ;;
esac
[[ $# -le 1 ]] || { usage >&2; exit 64; }

required_commands=(
  git make python3 sops wg wg-quick ansible ansible-playbook yamllint ansible-lint
)
if [[ "$apply" == true ]]; then
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

printf '%s\n' '4/4: checking pinned SSH connectivity without changing the host'
make fleet-platform-bootstrap-check \
  CONNECT=1 \
  PLATFORM_BUNDLE="$platform_bundle"

if [[ "$apply" != true ]]; then
  printf '%s\n' 'validation complete; no local or remote bootstrap changes were made'
  printf '%s\n' 'run scripts/platform-bootstrap.sh --apply to perform the bootstrap'
  exit 0
fi

[[ -t 0 && -t 1 ]] || {
  printf '%s\n' 'bootstrap apply requires an interactive terminal' >&2
  exit 67
}

cat <<EOF

The next step will modify the management VPS and install the managed local
WireGuard interface from commit $source_sha.
Vault will remain uninitialized. Type APPLY to continue.
EOF
read -r confirmation
[[ "$confirmation" == APPLY ]] || { printf '%s\n' 'bootstrap cancelled'; exit 68; }

sudo -v
make fleet-platform-bootstrap \
  APPLY=1 \
  PLATFORM_BUNDLE="$platform_bundle" \
  PLATFORM_WIREGUARD_PRIVATE_KEY="$operator_key"

sudo test -f /etc/wireguard/spiritvpn-mgmt.conf
ip link show dev spiritvpn-mgmt >/dev/null
sudo wg show spiritvpn-mgmt

cat <<'EOF'

Platform bootstrap and convergence verification completed.
Vault is reachable but uninitialized. Perform the Vault recovery ceremony only
after external storage for unseal shares and the initial root token is ready.
EOF
