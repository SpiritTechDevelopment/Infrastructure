#!/usr/bin/env bash
# Offline contract test for xray-api.sh using a tiny stateful fake Xray CLI.
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP="$(mktemp -d)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT
cat >"$TMP/xray" <<'PY'
#!/usr/bin/env python3
import json
import os
import sys
state_path = os.environ["FAKE_XRAY_STATE"]
try:
    with open(state_path, encoding="utf-8") as handle:
        state = json.load(handle)
except (FileNotFoundError, json.JSONDecodeError):
    state = {}
args = sys.argv[1:]
if args[:2] == ["api", "statsquery"]:
    for email in sorted(state):
        print(f'name: "user>>>{email}>>>traffic>>>uplink" value: 123')
elif args[:2] == ["api", "inbounduser"]:
    print(json.dumps({"users": [{"email": email} for email in sorted(state)]}))
elif args[:2] == ["api", "adu"]:
    with open(args[-1], encoding="utf-8") as handle:
        request = json.load(handle)
    for inbound in request["inbounds"]:
        for client in inbound["settings"]["clients"]:
            state[client["email"]] = client["id"]
    with open(state_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    print("ok")
elif args[:2] == ["api", "rmu"]:
    state.pop(args[-1], None)
    with open(state_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    print("ok")
else:
    print(f"unsupported fake Xray invocation: {args!r}", file=sys.stderr)
    raise SystemExit(2)
PY
chmod +x "$TMP/xray"
export PATH="$TMP:$PATH"
export FAKE_XRAY_STATE="$TMP/state.json"
TARGET="127.0.0.1:10085"
UUID="11111111-1111-4111-8111-111111111111"
EMAIL="offline-api-test"
"$SCRIPT_DIR/xray-api.sh" "$TARGET" ping >/dev/null
"$SCRIPT_DIR/xray-api.sh" "$TARGET" add "$UUID" "$EMAIL" >/dev/null
"$SCRIPT_DIR/xray-api.sh" "$TARGET" has "$EMAIL"
[[ "$("$SCRIPT_DIR/xray-api.sh" "$TARGET" emails)" == "$EMAIL" ]]
"$SCRIPT_DIR/xray-api.sh" "$TARGET" stats "$EMAIL" | grep -Fq -- "$EMAIL"
"$SCRIPT_DIR/xray-api.sh" "$TARGET" remove "$EMAIL" >/dev/null
set +e
"$SCRIPT_DIR/xray-api.sh" "$TARGET" has "$EMAIL"
rc=$?
set -e
[[ $rc -eq 1 ]] || { echo "expected absent user status 1, got $rc" >&2; exit 1; }
echo "offline Xray API wrapper test passed"
