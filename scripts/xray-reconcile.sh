#!/usr/bin/env bash
# Reconcile backend-owned desired runtime VLESS users into one Xray inbound.
set -Eeuo pipefail

TARGET="${1:-}"
STATE="${2:-}"
[[ $# -ge 2 ]] && shift 2 || true
TAG="vless-in"
PRUNE=0
REPLACE_EXISTING=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag) [[ $# -ge 2 ]] || { echo "--tag requires a value" >&2; exit 2; }; TAG="$2"; shift 2 ;;
    --prune) PRUNE=1; shift ;;
    --replace-existing) REPLACE_EXISTING=1; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
api() { "$here/xray-api.sh" "$TARGET" "$@"; }

[[ -n "$TARGET" && -n "$STATE" ]] \
  || fail "usage: $0 <inventory-host|host:port> <state.json> [--tag TAG] [--prune] [--replace-existing]"
[[ -f "$STATE" ]] || fail "state file not found: $STATE"

python3 - "$STATE" "$PRUNE" <<'PY'
import json, re, sys, uuid
path, prune = sys.argv[1], int(sys.argv[2])
with open(path, encoding="utf-8") as handle:
    data = json.load(handle)
users = data.get("users")
if not isinstance(users, list):
    raise SystemExit("state file must contain a 'users' list")
if not users and not prune:
    raise SystemExit("empty desired state is useful only with --prune")
seen_email, seen_uuid = set(), set()
for user in users:
    if not isinstance(user, dict):
        raise SystemExit(f"every user must be an object: {user!r}")
    try:
        parsed = str(uuid.UUID(user.get("uuid", "")))
    except (ValueError, AttributeError) as exc:
        raise SystemExit(f"invalid uuid in {user!r}: {exc}")
    email = str(user.get("email", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9._@:+-]{1,128}", email):
        raise SystemExit(f"unsupported email/accounting identifier: {email!r}")
    flow = str(user.get("flow", "xtls-rprx-vision"))
    if flow not in {"", "xtls-rprx-vision"}:
        raise SystemExit(f"unsupported flow for {email!r}: {flow!r}")
    if email.startswith("svc-entry-") or email.startswith("via-"):
        raise SystemExit(f"desired customer state must not own infrastructure identity: {email}")
    if email in seen_email:
        raise SystemExit(f"duplicate email: {email}")
    if parsed in seen_uuid:
        raise SystemExit(f"duplicate uuid: {parsed}")
    seen_email.add(email)
    seen_uuid.add(parsed)
PY

api ping >/dev/null || fail "API target $TARGET is not reachable"
CURRENT="$(api emails "$TAG")" || fail "could not list users on $TARGET"

added=0
present=0
replaced=0
while IFS=$'\t' read -r uuid_value email flow; do
  [[ -n "$email" ]] || continue
  if grep -Fxq -- "$email" <<<"$CURRENT"; then
    if (( REPLACE_EXISTING == 0 )); then
      present=$((present + 1))
      continue
    fi
    printf '[reconcile] replace %s\n' "$email" >&2
    api remove "$email" "$TAG" >/dev/null || fail "failed to remove existing $email"
    api add "$uuid_value" "$email" "$TAG" "$flow" >/dev/null || fail "failed to re-add $email"
    replaced=$((replaced + 1))
    continue
  fi
  printf '[reconcile] add %s\n' "$email" >&2
  api add "$uuid_value" "$email" "$TAG" "$flow" >/dev/null \
    || fail "failed to add $email"
  added=$((added + 1))
done < <(python3 - "$STATE" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    users = json.load(handle)["users"]
for user in users:
    print("\t".join((user["uuid"], user["email"], user.get("flow", "xtls-rprx-vision"))))
PY
)

# Re-list after add/replace so pruning uses the actual current state, not the initial snapshot.
CURRENT="$(api emails "$TAG")" || fail "could not re-list users after reconciliation"

pruned=0
protected=0
if (( PRUNE == 1 )); then
  DESIRED_EMAILS="$(python3 -c 'import json,sys; [print(u["email"]) for u in json.load(open(sys.argv[1]))["users"]]' "$STATE")"
  while IFS= read -r email; do
    [[ -n "$email" ]] || continue
    case "$email" in
      svc-entry-*|via-*)
        protected=$((protected + 1))
        continue
        ;;
    esac
    if ! grep -Fxq -- "$email" <<<"$DESIRED_EMAILS"; then
      printf '[reconcile] prune %s\n' "$email" >&2
      api remove "$email" "$TAG" >/dev/null || fail "failed to remove $email"
      pruned=$((pruned + 1))
    fi
  done <<<"$CURRENT"
fi

printf 'reconcile done: added=%d already_present=%d replaced=%d pruned=%d protected=%d\n' \
  "$added" "$present" "$replaced" "$pruned" "$protected"
