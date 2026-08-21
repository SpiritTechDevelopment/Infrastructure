#!/usr/bin/env bash
# Apply or verify a runner-local WireGuard config from an exact-SHA platform plan.
set -euo pipefail
umask 077

usage() {
  cat >&2 <<'USAGE'
usage: enroll-runner-overlay.sh --plan <private-runner-plan.yml> [--mode check|apply]

The plan must be generated from a clean committed checkout:

  python3 scripts/platform-sops.py runner-plan \
    --bundle inventories/bootstrap/platform.sops.yml \
    --runner-id <logical-runner-id> \
    --source-git-sha "$(git rev-parse HEAD)" \
    --output /tmp/runner-plan.yml

Run this script as root on the runner. Check is read-only. Apply creates a new
runner-local private key only when no config exists and the Git declaration is
still pending (its public_key is empty). An existing differing config is never
overwritten automatically.
USAGE
  exit 64
}

fail() {
  printf 'runner overlay enrollment failed: %s\n' "$*" >&2
  exit 1
}

plan=""
mode=check
while [[ $# -gt 0 ]]; do
  case "$1" in
    --plan) plan="${2:-}"; shift 2 ;;
    --mode) mode="${2:-}"; shift 2 ;;
    -h|--help) usage ;;
    *) usage ;;
  esac
done

[[ "$EUID" -eq 0 ]] || fail "root is required to inspect the protected WireGuard config"
[[ "$mode" =~ ^(check|apply)$ ]] || usage
[[ -n "$plan" && -f "$plan" && ! -L "$plan" ]] || fail "plan must be a regular file"
plan_mode="$(stat -c '%a' "$plan")"
(( (8#$plan_mode & 8#077) == 0 )) || fail "plan must not be readable by group or others"
for command_name in python3 wg systemctl install cmp stat mktemp sha256sum readlink awk; do
  command -v "$command_name" >/dev/null 2>&1 || fail "required command is unavailable: $command_name"
done

mapfile -d '' contract < <(
python3 - "$plan" <<'PY'
import base64
import binascii
import ipaddress
import re
import sys
from pathlib import Path

import yaml


def fail() -> None:
    raise SystemExit(64)


try:
    document = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, yaml.YAMLError):
    fail()
if not isinstance(document, dict) or set(document) != {
    "artifacts",
    "environment_network",
    "hub",
    "persistent_keepalive_seconds",
    "runner",
    "schema_version",
    "source_git_sha",
}:
    fail()
if document["schema_version"] != 1 or not re.fullmatch(
    r"[0-9a-f]{40}", str(document["source_git_sha"])
):
    fail()
artifacts = document["artifacts"]
if not isinstance(artifacts, dict) or set(artifacts) != {"enrollment_script_sha256"}:
    fail()
script_sha256 = artifacts["enrollment_script_sha256"]
if not isinstance(script_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", script_sha256):
    fail()
runner = document["runner"]
hub = document["hub"]
if not isinstance(runner, dict) or set(runner) != {
    "address",
    "environment",
    "id",
    "interface",
    "public_key",
}:
    fail()
if not isinstance(hub, dict) or set(hub) != {
    "endpoint",
    "overlay_address",
    "public_key",
}:
    fail()
if runner["environment"] not in {"develop", "prod"}:
    fail()
if not isinstance(runner["id"], str) or not re.fullmatch(
    r"[a-z0-9][a-z0-9-]{0,62}", runner["id"]
):
    fail()
if not isinstance(runner["interface"], str) or not re.fullmatch(
    r"[A-Za-z0-9_.-]{1,15}", runner["interface"]
):
    fail()
try:
    runner_address = ipaddress.ip_interface(runner["address"])
    hub_address = ipaddress.ip_address(hub["overlay_address"])
    environment_network = ipaddress.ip_network(document["environment_network"], strict=True)
except (TypeError, ValueError):
    fail()
if (
    runner_address.version != 4
    or runner_address.network.prefixlen != 32
    or hub_address.version != 4
    or environment_network.version != 4
    or runner_address.ip not in environment_network
    or hub_address not in environment_network
    or runner_address.ip == hub_address
):
    fail()


def wireguard_key(value: object, *, pending: bool) -> str:
    if pending and value == "":
        return ""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9+/]{43}=", value):
        fail()
    try:
        if len(base64.b64decode(value, validate=True)) != 32:
            fail()
    except (ValueError, binascii.Error):
        fail()
    return value


runner_public_key = wireguard_key(runner["public_key"], pending=True)
hub_public_key = wireguard_key(hub["public_key"], pending=False)
endpoint = hub["endpoint"]
if not isinstance(endpoint, str) or not re.fullmatch(
    r"(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\]):[0-9]{1,5}", endpoint
):
    fail()
keepalive = document["persistent_keepalive_seconds"]
if not isinstance(keepalive, int) or isinstance(keepalive, bool) or not 1 <= keepalive <= 65535:
    fail()

values = (
    script_sha256,
    runner["id"],
    runner["environment"],
    runner["interface"],
    str(runner_address),
    runner_public_key,
    hub_public_key,
    endpoint,
    str(hub_address),
    str(environment_network),
    str(keepalive),
    document["source_git_sha"],
)
for value in values:
    sys.stdout.buffer.write(value.encode("utf-8") + b"\0")
PY
)

[[ "${#contract[@]}" -eq 12 ]] || fail "Git-owned runner plan is incomplete"
expected_script_sha256="${contract[0]}"
runner_id="${contract[1]}"
environment="${contract[2]}"
interface="${contract[3]}"
runner_address="${contract[4]}"
expected_runner_public_key="${contract[5]}"
hub_public_key="${contract[6]}"
hub_endpoint="${contract[7]}"
hub_overlay_address="${contract[8]}"
environment_network="${contract[9]}"
keepalive="${contract[10]}"
source_git_sha="${contract[11]}"

script_path="$(readlink -f "$0")"
actual_script_sha256="$(sha256sum "$script_path" | awk '{print $1}')"
[[ "$actual_script_sha256" == "$expected_script_sha256" ]] \
  || fail "enrollment script does not belong to the plan's exact Git revision"

config_path="/etc/wireguard/${interface}.conf"

render_candidate() {
  local private_key="$1"
  local target="$2"
  {
    printf '%s\n' '[Interface]'
    printf '# spiritvpn runner: %s\n' "$runner_id"
    printf 'Address = %s\n' "$runner_address"
    printf 'PrivateKey = %s\n' "$private_key"
    printf '\n'
    printf '%s\n' '[Peer]'
    printf '# management hub\n'
    printf 'PublicKey = %s\n' "$hub_public_key"
    printf 'Endpoint = %s\n' "$hub_endpoint"
    printf 'AllowedIPs = %s/32\n' "$hub_overlay_address"
    printf 'PersistentKeepalive = %s\n' "$keepalive"
  } > "$target"
  chmod 0600 "$target"
}

private_key=""
if [[ -e "$config_path" ]]; then
  [[ -f "$config_path" && ! -L "$config_path" ]] \
    || fail "existing WireGuard config is not a safe regular file"
  [[ "$(stat -c '%u:%a' "$config_path")" == "0:600" ]] \
    || fail "existing WireGuard config must be root-owned mode 0600"
  private_key="$(
python3 - "$config_path" <<'PY'
import re
import sys
from pathlib import Path

matches = re.findall(
    r"(?m)^\s*PrivateKey\s*=\s*([A-Za-z0-9+/]{43}=)\s*$",
    Path(sys.argv[1]).read_text(encoding="utf-8"),
)
if len(matches) != 1:
    raise SystemExit(64)
print(matches[0])
PY
)" || fail "existing WireGuard config has no single private key"
else
  [[ "$mode" == apply ]] || fail "runner WireGuard config is absent"
  [[ -z "$expected_runner_public_key" ]] || fail \
    "Git declares a runner key but its matching local private key is absent"
  private_key="$(wg genkey)"
fi

actual_runner_public_key="$(printf '%s' "$private_key" | wg pubkey)"
if [[ -n "$expected_runner_public_key" && \
      "$actual_runner_public_key" != "$expected_runner_public_key" ]]; then
  fail "local runner identity differs from the Git-owned public key"
fi

temporary="$(mktemp "/tmp/spiritvpn-runner-${interface}.candidate.XXXXXX")"
cleanup() {
  rm -f -- "$temporary"
}
trap cleanup EXIT
render_candidate "$private_key" "$temporary"
private_key=""

if [[ -e "$config_path" ]]; then
  cmp --silent "$temporary" "$config_path" || fail \
    "existing runner config differs from the exact Git-owned plan; refusing overwrite"
elif [[ "$mode" == apply ]]; then
  install -d -o root -g root -m 0700 /etc/wireguard
  install -o root -g root -m 0600 "$temporary" "$config_path"
fi

if [[ "$mode" == apply ]]; then
  systemctl enable --now "wg-quick@${interface}.service" >/dev/null 2>&1 \
    || fail "runner WireGuard service did not start"
else
  systemctl is-enabled --quiet "wg-quick@${interface}.service" \
    || fail "runner WireGuard service is not enabled"
  systemctl is-active --quiet "wg-quick@${interface}.service" \
    || fail "runner WireGuard service is not active"
fi
wg show "$interface" >/dev/null 2>&1 || fail "runner WireGuard interface is unavailable"

if [[ -z "$expected_runner_public_key" ]]; then
  printf '%s\n' 'runner identity is pending in the encrypted platform contract'
  printf 'runner_public_key=%s\n' "$actual_runner_public_key"
  printf '%s\n' 'Add this public key to the declared runner peer, commit it, then run the guarded platform refresh.'
else
  printf 'runner overlay %s: match (%s)\n' "$runner_id" "$mode"
fi
