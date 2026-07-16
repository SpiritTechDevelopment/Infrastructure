#!/usr/bin/env bash
# Generate a validated VLESS + REALITY client profile for any deployed entry/exit.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

NODE="entry-1"
INVENTORY="$REPO_ROOT/inventories/prod/inventory.yml"
UUID=""
EMAIL=""
REALITY_PASSWORD=""
API=""
OUT=""
HOST_OVERRIDE=""
SOCKS_PORT="10808"
METADATA="$REPO_ROOT/generated/client-endpoints.json"
ALLOW_SSH=1

usage() {
  cat <<USAGE
Usage: $0 --uuid UUID --email ID [options]

Options:
  --node HOST          Inventory host (default: entry-1)
  --entry HOST         Backward-compatible alias for --node
  --inventory PATH     Inventory path
  --password VALUE     Use this REALITY client password instead of deriving it over SSH
  --pubkey VALUE       Backward-compatible alias for --password
  --api HOST|HOST:PORT Verify the runtime user exists before emitting
  --out FILE           Write client JSON to FILE
  --host HOSTNAME|IP   Override the public connection address
  --socks-port PORT     Local SOCKS listen port in generated JSON (default: 10808)
  --metadata FILE       Deployed client endpoint manifest (default: generated/client-endpoints.json)
  --no-ssh              Fail rather than derive the REALITY client password over SSH
USAGE
}

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --node|--entry) NODE="$2"; shift 2 ;;
    --inventory) INVENTORY="$2"; shift 2 ;;
    --uuid) UUID="$2"; shift 2 ;;
    --email) EMAIL="$2"; shift 2 ;;
    --password|--pubkey) REALITY_PASSWORD="$2"; shift 2 ;;
    --api) API="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --host) HOST_OVERRIDE="$2"; shift 2 ;;
    --socks-port) SOCKS_PORT="$2"; shift 2 ;;
    --metadata) METADATA="$2"; shift 2 ;;
    --no-ssh) ALLOW_SSH=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown argument: $1" ;;
  esac
done

[[ -n "$UUID" ]] || fail "--uuid is required"
[[ -n "$EMAIL" ]] || fail "--email is required"
[[ "$SOCKS_PORT" =~ ^[0-9]+$ ]] || fail "--socks-port must be numeric"
(( SOCKS_PORT >= 1024 && SOCKS_PORT <= 65535 )) || fail "--socks-port must be between 1024 and 65535"
python3 - "$UUID" <<'PY'
import sys, uuid
try:
    uuid.UUID(sys.argv[1])
except ValueError as exc:
    raise SystemExit(f"invalid UUID: {exc}")
PY
command -v ansible-inventory >/dev/null 2>&1 || fail "ansible-inventory not found"
[[ -f "$INVENTORY" ]] || fail "inventory not found: $INVENTORY"

INVENTORY_JSON="$(ansible-inventory -i "$INVENTORY" --host "$NODE")" \
  || fail "cannot read inventory host $NODE"
FACTS="$(INVENTORY_JSON="$INVENTORY_JSON" python3 - "$HOST_OVERRIDE" <<'PY'
import json, os, sys
override = sys.argv[1]
h = json.loads(os.environ["INVENTORY_JSON"])
def need(key):
    value = h.get(key)
    if value in (None, "", [], {}):
        raise SystemExit(f"missing required inventory value: {key}")
    return value
public_host = override or h.get("public_hostname") or h.get("ansible_host")
if not public_host:
    raise SystemExit("missing public_hostname/ansible_host")
print(json.dumps({
    "public_host": public_host,
    "public_port": int(h.get("public_port", h.get("xray_listen_port", 443))),
    "sni": need("reality_server_names")[0],
    "short_id": (h.get("reality_short_ids") or [""])[0],
    "ssh_host": need("ansible_host"),
    "ssh_port": int(h.get("ansible_port", 22)),
    "ssh_user": h.get("ansible_user", "root"),
    "ssh_key": os.path.expanduser(h.get("ansible_ssh_private_key_file", "")),
    "xray_image": h.get("xray_image", "ghcr.io/xtls/xray-core:26.3.27"),
}))
PY
)" || fail "could not resolve client facts for $NODE"

fact() { python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1" <<<"$FACTS"; }
PUBLIC_HOST="$(fact public_host)"
PUBLIC_PORT="$(fact public_port)"
SNI="$(fact sni)"
SHORT_ID="$(fact short_id)"
SSH_HOST="$(fact ssh_host)"
SSH_PORT="$(fact ssh_port)"
SSH_USER="$(fact ssh_user)"
SSH_KEY="$(fact ssh_key)"
XRAY_IMAGE_NODE="$(fact xray_image)"

if [[ -z "$REALITY_PASSWORD" && -f "$METADATA" ]]; then
  REALITY_PASSWORD="$(python3 - "$METADATA" "$NODE" "$PUBLIC_HOST" "$PUBLIC_PORT" "$SNI" "$SHORT_ID" "$HOST_OVERRIDE" <<'PY'
import json, sys
path, node, host, port, sni, sid, host_override = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    manifest = json.load(handle)
if manifest.get("schema_version") != 1:
    raise SystemExit("unsupported client metadata schema")
entry = manifest.get("entries", {}).get(node)
if not isinstance(entry, dict):
    raise SystemExit(f"entry {node!r} is absent from client metadata")
checks = {
    "port": int(port),
    "server_name": sni,
    "short_id": sid,
}
if not host_override:
    checks["address"] = host
for key, expected in checks.items():
    if entry.get(key) != expected:
        raise SystemExit(f"stale client metadata for {node}: {key}={entry.get(key)!r}, expected {expected!r}")
print(entry.get("reality_password", ""))
PY
  )" || fail "could not use client metadata: $METADATA"
  echo "Using deployed client metadata for $NODE" >&2
fi

if [[ -z "$REALITY_PASSWORD" ]]; then
  (( ALLOW_SSH == 1 )) || fail "client metadata is missing and --no-ssh was requested"
  ssh_args=(-o BatchMode=yes -o IdentitiesOnly=yes -p "$SSH_PORT")
  [[ -n "$SSH_KEY" ]] && ssh_args+=(-i "$SSH_KEY")
  echo "Deriving $NODE REALITY client password over SSH ..." >&2
  REALITY_PASSWORD="$(
    remote_command="env XRAY_IMAGE='$XRAY_IMAGE_NODE' bash -s"
    [[ "$SSH_USER" == root ]] || remote_command="sudo -n $remote_command"
    ssh "${ssh_args[@]}" "${SSH_USER}@${SSH_HOST}" "$remote_command" <<'REMOTE'
set -Eeuo pipefail
priv="$(cat /var/lib/xray/reality.key)"
out="$(docker run --rm "$XRAY_IMAGE" x25519 -i "$priv" 2>&1)"
pub="$(printf '%s\n' "$out" | sed -nE 's/^[[:space:]]*(Password([[:space:]]*\([[:space:]]*Public[[:space:]]*[Kk]ey[[:space:]]*\))?|Public[[:space:]]*[Kk]ey)[[:space:]]*[:=][[:space:]]*([A-Za-z0-9_-]{43}).*/\3/p' | head -n1)"
[[ "$pub" =~ ^[A-Za-z0-9_-]{43}$ ]] || { echo "could not parse REALITY client password" >&2; exit 1; }
[[ "$pub" != "$priv" ]] || { echo "derived value equals private key" >&2; exit 1; }
printf '%s\n' "$pub"
REMOTE
  )" || fail "failed to derive REALITY client password from $NODE"
fi
[[ "$REALITY_PASSWORD" =~ ^[A-Za-z0-9_-]{43}$ ]] || fail "malformed REALITY client password"

if [[ -n "$API" ]]; then
  set +e
  XRAY_INVENTORY="$INVENTORY" "$SCRIPT_DIR/xray-api.sh" "$API" has "$EMAIL"
  api_user_rc=$?
  set -e
  if (( api_user_rc == 1 )); then
    fail "exact user '$EMAIL' is not present on API target '$API'"
  elif (( api_user_rc != 0 )); then
    fail "could not query API target '$API' while checking '$EMAIL'"
  fi
fi

VLESS_URI="$(python3 - "$UUID" "$PUBLIC_HOST" "$PUBLIC_PORT" "$SNI" "$SHORT_ID" "$REALITY_PASSWORD" "$NODE" <<'PY'
import sys
from urllib.parse import quote, urlencode
uid, host, port, sni, sid, pbk, node = sys.argv[1:]
params = {
    "encryption": "none",
    "flow": "xtls-rprx-vision",
    "security": "reality",
    "sni": sni,
    "fp": "chrome",
    "pbk": pbk,
    "sid": sid,
    "type": "tcp",
}
print(f"vless://{uid}@{host}:{port}?{urlencode(params)}#{quote('Spirit VPN ' + node)}")
PY
)"

if [[ -n "$OUT" ]]; then
  mkdir -p "$(dirname "$OUT")"
  python3 - "$OUT" "$UUID" "$PUBLIC_HOST" "$PUBLIC_PORT" "$SNI" "$SHORT_ID" "$REALITY_PASSWORD" "$SOCKS_PORT" <<'PY'
import json, sys
out, uid, host, port, sni, sid, pbk, socks_port = sys.argv[1:]
config = {
    "log": {"loglevel": "warning"},
    "inbounds": [{
        "listen": "127.0.0.1",
        "port": int(socks_port),
        "protocol": "socks",
        "settings": {"udp": True},
    }],
    "outbounds": [{
        "protocol": "vless",
        "settings": {"vnext": [{
            "address": host,
            "port": int(port),
            "users": [{"id": uid, "flow": "xtls-rprx-vision", "encryption": "none"}],
        }]},
        "streamSettings": {
            "network": "tcp",
            "security": "reality",
            "realitySettings": {
                "fingerprint": "chrome",
                "serverName": sni,
                "password": pbk,
                "shortId": sid,
            },
        },
    }],
}
with open(out, "w", encoding="utf-8") as handle:
    json.dump(config, handle, indent=2)
print(f"Wrote client config: {out}", file=sys.stderr)
PY
fi

printf '%s\n' "$VLESS_URI"
