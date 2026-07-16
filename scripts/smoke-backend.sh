#!/usr/bin/env bash
# Full backend contract test:
#   public HandlerService -> add unique user -> generate client -> customer tunnel
#   -> configured default exit -> Internet -> per-user stats -> remove user -> reject.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ENTRY="entry-1"
EXIT_HOST=""
INVENTORY="$REPO_ROOT/inventories/prod/inventory.yml"
XRAY_IMAGE_OVERRIDE="${XRAY_IMAGE:-}"
XRAY_IMAGE="ghcr.io/xtls/xray-core:26.3.27"
E2E_IP_CHECK_URLS="${E2E_IP_CHECK_URLS:-https://api.ipify.org https://checkip.amazonaws.com}"
E2E_REJECTION_SECONDS="${E2E_REJECTION_SECONDS:-15}"

usage() {
  cat <<USAGE
Usage: $0 [--entry HOST] [--exit HOST] [--inventory PATH]

Defaults:
  entry: entry-1
  exit:  resolved from entry_default_exit_tag

Optional environment:
  E2E_IP_CHECK_URLS="URL1 URL2"  Public-IP services tried in order
  E2E_REJECTION_SECONDS=15       Fresh-connection rejection window
USAGE
}

fail() {
  printf 'E2E FAIL: %s\n' "$*" >&2
  if [[ -n "${CLIENT_LOG:-}" && -f "$CLIENT_LOG" ]]; then
    printf '%s\n' '--- local Xray client log ---' >&2
    tail -n 100 "$CLIENT_LOG" >&2 || true
  fi
  exit 1
}
info() { printf '[e2e] %s\n' "$*" >&2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --entry) ENTRY="$2"; shift 2 ;;
    --exit) EXIT_HOST="$2"; shift 2 ;;
    --inventory) INVENTORY="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown argument: $1" ;;
  esac
done

[[ "$E2E_REJECTION_SECONDS" =~ ^[1-9][0-9]*$ ]] \
  || fail "E2E_REJECTION_SECONDS must be a positive integer"
command -v ansible-inventory >/dev/null 2>&1 || fail "ansible-inventory not found"
command -v curl >/dev/null 2>&1 || fail "curl not found"
command -v python3 >/dev/null 2>&1 || fail "python3 not found"
[[ -f "$INVENTORY" ]] || fail "inventory not found: $INVENTORY"
[[ -x "$SCRIPT_DIR/xray-api.sh" ]] || fail "xray-api.sh is missing"

inventory_host_json() {
  ansible-inventory -i "$INVENTORY" --host "$1" 2>/dev/null
}

ENTRY_JSON="$(inventory_host_json "$ENTRY")" || fail "cannot resolve entry host: $ENTRY"
readarray -t ENTRY_FACTS < <(INVENTORY_JSON="$ENTRY_JSON" python3 - <<'PY'
import json, os
h = json.loads(os.environ["INVENTORY_JSON"])
print(h.get("entry_default_exit_tag", ""))
print(h.get("xray_image", "ghcr.io/xtls/xray-core:26.3.27"))
PY
)
ENTRY_DEFAULT_TAG="${ENTRY_FACTS[0]:-}"
XRAY_IMAGE="${XRAY_IMAGE_OVERRIDE:-${ENTRY_FACTS[1]:-ghcr.io/xtls/xray-core:26.3.27}}"
[[ -n "$ENTRY_DEFAULT_TAG" ]] || fail "$ENTRY has no entry_default_exit_tag"

if [[ -z "$EXIT_HOST" ]]; then
  INVENTORY_LIST_JSON="$(ansible-inventory -i "$INVENTORY" --list 2>/dev/null)" \
    || fail "cannot read inventory"
  EXIT_HOST="$(INVENTORY_JSON="$INVENTORY_LIST_JSON" DEFAULT_TAG="$ENTRY_DEFAULT_TAG" python3 - <<'PY'
import json, os
inventory = json.loads(os.environ["INVENTORY_JSON"])
tag = os.environ["DEFAULT_TAG"]
hostvars = inventory.get("_meta", {}).get("hostvars", {})
for name, values in hostvars.items():
    if values.get("node_enabled", True) is False:
        continue
    country = values.get("country")
    if country and f"{country}-exit" == tag:
        print(name)
        break
else:
    raise SystemExit(f"no enabled exit matches default tag {tag!r}")
PY
)" || fail "cannot map $ENTRY_DEFAULT_TAG to an enabled exit"
fi

EXIT_JSON="$(inventory_host_json "$EXIT_HOST")" || fail "cannot resolve exit host: $EXIT_HOST"
readarray -t EXIT_FACTS < <(INVENTORY_JSON="$EXIT_JSON" python3 - <<'PY'
import json, os
h = json.loads(os.environ["INVENTORY_JSON"])
country = h.get("country", "")
tag = f"{country}-exit" if country else ""
expected = h.get("expected_egress_ip") or h.get("ansible_host") or ""
print(tag)
print(expected)
print(country)
PY
)
EXIT_TAG="${EXIT_FACTS[0]:-}"
EXPECTED_IP="${EXIT_FACTS[1]:-}"
EXIT_COUNTRY="${EXIT_FACTS[2]:-}"
[[ -n "$EXIT_TAG" ]] || fail "$EXIT_HOST has no country/tag"
[[ -n "$EXIT_COUNTRY" ]] || fail "$EXIT_HOST has no country"
[[ -n "$EXPECTED_IP" ]] || fail "$EXIT_HOST has no expected_egress_ip/ansible_host"
python3 - "$EXPECTED_IP" <<'PY' || fail "$EXIT_HOST expected egress is not an IP address"
import ipaddress, sys
ipaddress.ip_address(sys.argv[1])
PY

SMOKE_UUID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
if [[ "$EXIT_TAG" == "$ENTRY_DEFAULT_TAG" ]]; then
  ROUTE_MODE="default-route"
  SMOKE_EMAIL="e2e-$(date +%s)-${SMOKE_UUID%%-*}@smoke.invalid"
else
  ROUTE_MODE="selector-route"
  SMOKE_EMAIL="via-${EXIT_COUNTRY}"
fi
SOCKS_PORT="$(python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"
CLIENT_CFG="$(mktemp --suffix=.json)"
CLIENT_LOG="$(mktemp --suffix=.log)"
CLIENT_MODE=""
CLIENT_PID=""
CLIENT_CONTAINER="spirit-e2e-$$"
USER_ADDED=0
USER_REMOVED=0

stop_client() {
  if [[ "$CLIENT_MODE" == docker ]]; then
    docker stop -t 2 "$CLIENT_CONTAINER" >/dev/null 2>&1 || true
  fi
  if [[ -n "$CLIENT_PID" ]]; then
    kill "$CLIENT_PID" >/dev/null 2>&1 || true
    wait "$CLIENT_PID" >/dev/null 2>&1 || true
  fi
  CLIENT_PID=""
  CLIENT_MODE=""
}

cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  stop_client
  if (( USER_ADDED == 1 && USER_REMOVED == 0 )); then
    XRAY_INVENTORY="$INVENTORY" "$SCRIPT_DIR/xray-api.sh" "$ENTRY" remove "$SMOKE_EMAIL" >/dev/null 2>&1 || true
  fi
  rm -f "$CLIENT_CFG" "$CLIENT_LOG"
  exit "$rc"
}
trap cleanup EXIT INT TERM

start_client() {
  : >"$CLIENT_LOG"
  if command -v xray >/dev/null 2>&1; then
    CLIENT_MODE=xray
    xray run -c "$CLIENT_CFG" >"$CLIENT_LOG" 2>&1 &
    CLIENT_PID=$!
  elif command -v docker >/dev/null 2>&1; then
    CLIENT_MODE=docker
    docker rm -f "$CLIENT_CONTAINER" >/dev/null 2>&1 || true
    docker run --rm --user 0:0 --name "$CLIENT_CONTAINER" --network host \
      -v "$CLIENT_CFG:/client.json:ro" \
      "$XRAY_IMAGE" run -c /client.json >"$CLIENT_LOG" 2>&1 &
    CLIENT_PID=$!
  else
    fail "install a local Xray binary or Docker on the deployment controller"
  fi
}

normalize_ip() {
  python3 -c 'import ipaddress,sys; value=sys.stdin.read().strip(); print(ipaddress.ip_address(value))' 2>/dev/null
}

proxy_egress() {
  local url raw normalized
  for url in $E2E_IP_CHECK_URLS; do
    raw="$(curl -fsS --max-time 12 --connect-timeout 5 \
      --socks5-hostname "127.0.0.1:$SOCKS_PORT" "$url" 2>/dev/null || true)"
    [[ -n "$raw" ]] || continue
    normalized="$(printf '%s' "$raw" | normalize_ip || true)"
    if [[ -n "$normalized" ]]; then
      printf '%s\n' "$normalized"
      return 0
    fi
  done
  return 1
}

api() { XRAY_INVENTORY="$INVENTORY" "$SCRIPT_DIR/xray-api.sh" "$ENTRY" "$@"; }

info "checking public Xray API on $ENTRY; route=$ROUTE_MODE target=$EXIT_HOST ($EXIT_TAG)"
api ping >/dev/null || fail "public Xray API for $ENTRY is unreachable"
set +e
api has "$SMOKE_EMAIL"
preexisting_rc=$?
set -e
if (( preexisting_rc == 0 )); then
  fail "generated smoke identity unexpectedly already exists"
elif (( preexisting_rc != 1 )); then
  fail "could not list users before the E2E add"
fi

info "adding unique runtime user $SMOKE_EMAIL"
api add "$SMOKE_UUID" "$SMOKE_EMAIL" >/dev/null \
  || fail "HandlerService could not add the runtime user"
USER_ADDED=1
api has "$SMOKE_EMAIL" || fail "runtime user is absent immediately after add"

info "generating a client for the API-created user"
"$SCRIPT_DIR/gen-client.sh" \
  --node "$ENTRY" \
  --inventory "$INVENTORY" \
  --uuid "$SMOKE_UUID" \
  --email "$SMOKE_EMAIL" \
  --api "$ENTRY" \
  --socks-port "$SOCKS_PORT" \
  --no-ssh --out "$CLIENT_CFG" >/dev/null \
  || fail "client profile generation failed"

info "starting customer client on SOCKS 127.0.0.1:$SOCKS_PORT"
start_client

EGRESS=""
for _ in $(seq 1 15); do
  if [[ -n "$CLIENT_PID" ]] && ! kill -0 "$CLIENT_PID" 2>/dev/null; then
    fail "local Xray client exited before the tunnel became usable"
  fi
  EGRESS="$(proxy_egress 2>/dev/null || true)"
  [[ -n "$EGRESS" ]] && break
  sleep 2
done
[[ -n "$EGRESS" ]] || fail "the API-created user could not establish a tunnel"
info "observed egress IP: $EGRESS"
[[ "$EGRESS" == "$EXPECTED_IP" ]] \
  || fail "traffic exited as $EGRESS; expected $EXPECTED_IP through $EXIT_HOST"

info "waiting for per-user traffic counters"
STATS_SEEN=0
for _ in $(seq 1 15); do
  if api stats 2>/dev/null | grep -Fq -- "$SMOKE_EMAIL"; then
    STATS_SEEN=1
    break
  fi
  proxy_egress >/dev/null 2>&1 || true
  sleep 1
done
(( STATS_SEEN == 1 )) || fail "StatsService has no counters for $SMOKE_EMAIL"

info "removing runtime user and proving a fresh connection is rejected"
stop_client
api remove "$SMOKE_EMAIL" >/dev/null \
  || fail "HandlerService could not remove the runtime user"
USER_REMOVED=1
set +e
api has "$SMOKE_EMAIL"
removed_rc=$?
set -e
if (( removed_rc == 0 )); then
  fail "runtime user is still listed after removal"
elif (( removed_rc != 1 )); then
  fail "could not verify the runtime user removal"
fi

# The same config and local runtime already worked immediately above. The only state
# change is HandlerService removal. A full rejection window prevents a slow client
# startup from being mistaken for successful revocation.
start_client
for _ in $(seq 1 "$E2E_REJECTION_SECONDS"); do
  if [[ -n "$CLIENT_PID" ]] && ! kill -0 "$CLIENT_PID" 2>/dev/null; then
    info "local Xray client exited after user removal; fresh connection was rejected"
    break
  fi
  if rejected_egress="$(proxy_egress 2>/dev/null)"; then
    fail "removed user established a fresh tunnel with egress $rejected_egress"
  fi
  sleep 1
done
stop_client

printf 'E2E PASS: API add -> %s -> %s (%s) -> egress %s -> stats -> remove -> no fresh tunnel for %ss\n' \
  "$ENTRY" "$EXIT_HOST" "$ROUTE_MODE" "$EGRESS" "$E2E_REJECTION_SECONDS"
