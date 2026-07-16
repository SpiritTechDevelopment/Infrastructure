#!/usr/bin/env bash
# Backend/operator wrapper for Xray's gRPC API.
# The first argument may be either an inventory host (entry-1, exit-fr) or host:port.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DEFAULT_IMAGE="ghcr.io/xtls/xray-core:26.3.27"
IMAGE_OVERRIDE="${XRAY_IMAGE:-}"
IMAGE="$DEFAULT_IMAGE"
INBOUND_TAG="${XRAY_INBOUND_TAG:-vless-in}"
INVENTORY="${XRAY_INVENTORY:-$REPO_ROOT/inventories/prod/inventory.yml}"
API_TIMEOUT="${XRAY_API_TIMEOUT:-20}"
# Inner Xray gRPC call timeout (xray api -timeout). Default 3s is too tight once
# the API is reached over the WireGuard overlay (spoke-to-spoke via the hub adds
# latency); 10s absorbs that. The outer API_TIMEOUT still caps the whole call.
GRPC_TIMEOUT="${XRAY_GRPC_TIMEOUT:-10}"
USERS_PARSER="$SCRIPT_DIR/xray-users.py"

usage() {
  cat <<USAGE
Usage:
  $0 <inventory-host|host:port> ping
  $0 <inventory-host|host:port> list [inbound-tag]
  $0 <inventory-host|host:port> emails [inbound-tag]
  $0 <inventory-host|host:port> has <email> [inbound-tag]
  $0 <inventory-host|host:port> add <uuid> <email> [inbound-tag] [flow]
  $0 <inventory-host|host:port> remove <email> [inbound-tag]
  $0 <inventory-host|host:port> stats [pattern]

Examples:
  $0 entry-1 ping
  $0 entry-1 add "$(printf 'UUID')" user-123
  $0 5.101.67.252:10085 list
USAGE
}

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

resolve_target() {
  local value="$1"
  if [[ "$value" =~ ^\[[^]]+\]:[0-9]+$ || "$value" =~ ^[^:]+:[0-9]+$ ]]; then
    printf '%s\n%s\n' "$value" "${IMAGE_OVERRIDE:-$DEFAULT_IMAGE}"
    return
  fi
  command -v ansible-inventory >/dev/null 2>&1 \
    || fail "'$value' is not host:port and ansible-inventory is unavailable"
  [[ -f "$INVENTORY" ]] || fail "inventory not found: $INVENTORY"
  local inventory_json
  inventory_json="$(ansible-inventory -i "$INVENTORY" --host "$value" 2>/dev/null)" \
    || fail "cannot read inventory host: $value"
  INVENTORY_JSON="$inventory_json" IMAGE_OVERRIDE="$IMAGE_OVERRIDE" DEFAULT_IMAGE="$DEFAULT_IMAGE" python3 - "$value" <<'PY'
import ipaddress, json, os, sys
name = sys.argv[1]
try:
    h = json.loads(os.environ["INVENTORY_JSON"])
except Exception as exc:
    raise SystemExit(f"cannot read inventory host {name}: {exc}")
host = h.get("xray_api_overlay_host") or h.get("xray_api_public_host") or h.get("ansible_host")
port = int(h.get("xray_api_port", 10085))
if not host:
    raise SystemExit(f"inventory host {name} has no xray_api_overlay_host/xray_api_public_host/ansible_host")
try:
    is_v6 = ipaddress.ip_address(str(host)).version == 6
except ValueError:
    is_v6 = ":" in str(host)
endpoint = f"[{host}]:{port}" if is_v6 else f"{host}:{port}"
image = os.environ.get("IMAGE_OVERRIDE") or h.get("xray_image") or os.environ["DEFAULT_IMAGE"]
print(endpoint)
print(image)
PY
}

run_xray() {
  if command -v xray >/dev/null 2>&1; then
    timeout "$API_TIMEOUT" xray "$@"
  elif command -v docker >/dev/null 2>&1; then
    timeout "$API_TIMEOUT" docker run --rm --user 0:0 --network host "$IMAGE" "$@"
  else
    fail "install Xray or Docker on this Linux controller"
  fi
}

run_xray_with_file() {
  local file="$1"
  shift
  if command -v xray >/dev/null 2>&1; then
    timeout "$API_TIMEOUT" xray "$@" "$file"
  elif command -v docker >/dev/null 2>&1; then
    timeout "$API_TIMEOUT" docker run --rm --user 0:0 --network host \
      -v "$file:/request.json:ro" \
      "$IMAGE" "$@" /request.json
  else
    fail "install Xray or Docker on this Linux controller"
  fi
}

inbound_users_raw() {
  local tag="$1"
  run_xray api inbounduser --server="$ENDPOINT" -timeout "$GRPC_TIMEOUT" -tag="$tag"
}

has_exact_user() {
  local email="$1" tag="$2" output
  output="$(inbound_users_raw "$tag")" || return 2
  printf '%s\n' "$output" | "$USERS_PARSER" has "$email"
}

[[ "$API_TIMEOUT" =~ ^[1-9][0-9]*$ ]] || fail "XRAY_API_TIMEOUT must be a positive integer"
[[ -x "$USERS_PARSER" ]] || fail "missing parser: $USERS_PARSER"
[[ $# -ge 2 ]] || { usage >&2; exit 2; }
TARGET="$1"
ACTION="$2"
shift 2
readarray -t TARGET_FACTS < <(resolve_target "$TARGET")
ENDPOINT="${TARGET_FACTS[0]:-}"
IMAGE="${TARGET_FACTS[1]:-$DEFAULT_IMAGE}"
[[ -n "$ENDPOINT" ]] || fail "could not resolve API endpoint for $TARGET"

case "$ACTION" in
  ping)
    run_xray api statsquery --server="$ENDPOINT" -timeout "$GRPC_TIMEOUT" >/dev/null
    echo "Xray API reachable at $ENDPOINT"
    ;;

  list)
    inbound_users_raw "${1:-$INBOUND_TAG}"
    ;;

  emails)
    inbound_users_raw "${1:-$INBOUND_TAG}" | "$USERS_PARSER" list
    ;;

  has)
    [[ $# -ge 1 ]] || { usage >&2; exit 2; }
    email="$1"
    tag="${2:-$INBOUND_TAG}"
    [[ "$email" =~ ^[A-Za-z0-9._@:+-]{1,128}$ ]] \
      || fail "email/accounting identifier contains unsupported characters"
    has_exact_user "$email" "$tag"
    ;;

  add)
    [[ $# -ge 2 ]] || { usage >&2; exit 2; }
    uuid="$1"
    email="$2"
    tag="${3:-$INBOUND_TAG}"
    flow="${4:-xtls-rprx-vision}"
    [[ -z "$flow" || "$flow" == "xtls-rprx-vision" ]] \
      || fail "unsupported VLESS flow: $flow"
    python3 - "$uuid" "$email" <<'PY'
import re, sys, uuid
try:
    uuid.UUID(sys.argv[1])
except ValueError as exc:
    raise SystemExit(f"invalid UUID: {exc}")
email = sys.argv[2]
if not re.fullmatch(r"[A-Za-z0-9._@:+-]{1,128}", email):
    raise SystemExit("email/accounting identifier must be 1-128 safe ASCII characters")
PY
    set +e
    has_exact_user "$email" "$tag"
    existing_rc=$?
    set -e
    if (( existing_rc == 0 )); then
      fail "exact user '$email' already exists on $TARGET; refusing to create a duplicate"
    elif (( existing_rc != 1 )); then
      fail "could not list users before adding '$email'"
    fi

    request="$(mktemp --suffix=.json)"
    trap 'rm -f "$request"' EXIT
    chmod 0600 "$request"
    python3 - "$request" "$uuid" "$email" "$tag" "$flow" <<'PY'
import json, sys
path, uid, email, tag, flow = sys.argv[1:]
data = {
    "inbounds": [{
        "tag": tag,
        "listen": "127.0.0.1",
        "port": 2000,
        "protocol": "vless",
        "settings": {
            "decryption": "none",
            "clients": [{"id": uid, "email": email, "flow": flow}],
        },
    }]
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(data, handle)
PY
    run_xray_with_file "$request" api adu --server="$ENDPOINT" -timeout "$GRPC_TIMEOUT"
    set +e
    has_exact_user "$email" "$tag"
    verify_rc=$?
    set -e
    if (( verify_rc == 1 )); then
      fail "API returned success but exact user '$email' was not listed"
    elif (( verify_rc != 0 )); then
      fail "API returned success but user listing failed while verifying '$email'"
    fi
    echo "Added $email to $TARGET ($ENDPOINT), inbound=$tag"
    ;;

  remove)
    [[ $# -ge 1 ]] || { usage >&2; exit 2; }
    email="$1"
    tag="${2:-$INBOUND_TAG}"
    [[ "$email" =~ ^[A-Za-z0-9._@:+-]{1,128}$ ]] \
      || fail "email/accounting identifier contains unsupported characters"
    set +e
    has_exact_user "$email" "$tag"
    before_rc=$?
    set -e
    if (( before_rc == 1 )); then
      echo "User $email is already absent from $TARGET ($ENDPOINT), inbound=$tag"
      exit 0
    elif (( before_rc != 0 )); then
      fail "could not list users before removing '$email'"
    fi
    run_xray api rmu --server="$ENDPOINT" -timeout "$GRPC_TIMEOUT" -tag="$tag" "$email"
    set +e
    has_exact_user "$email" "$tag"
    verify_rc=$?
    set -e
    if (( verify_rc == 0 )); then
      fail "exact user '$email' is still present after removal"
    elif (( verify_rc != 1 )); then
      fail "could not verify removal of '$email'"
    fi
    echo "Removed $email from $TARGET ($ENDPOINT), inbound=$tag"
    ;;

  stats)
    output="$(run_xray api statsquery --server="$ENDPOINT" -timeout "$GRPC_TIMEOUT")"
    if [[ $# -gt 0 ]]; then
      printf '%s\n' "$output" | grep -F -- "$1" || true
    else
      printf '%s\n' "$output"
    fi
    ;;

  *)
    usage >&2
    exit 2
    ;;
esac
