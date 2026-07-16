#!/usr/bin/env bash
# Run the backend/customer E2E once for every enabled exit.
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENTRY="entry-1"
INVENTORY="$REPO_ROOT/inventories/prod/inventory.yml"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --entry) ENTRY="$2"; shift 2 ;;
    --inventory) INVENTORY="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 [--entry HOST] [--inventory PATH]"
      exit 0
      ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
command -v ansible-inventory >/dev/null 2>&1 || { echo "ansible-inventory not found" >&2; exit 1; }
[[ -f "$INVENTORY" ]] || { echo "inventory not found: $INVENTORY" >&2; exit 1; }
LIST="$(ansible-inventory -i "$INVENTORY" --list)"
mapfile -t EXITS < <(INVENTORY_JSON="$LIST" python3 - <<'PY'
import json, os
inventory = json.loads(os.environ["INVENTORY_JSON"])
hostvars = inventory.get("_meta", {}).get("hostvars", {})
for host in inventory.get("exit", {}).get("hosts", []):
    if hostvars.get(host, {}).get("node_enabled", True):
        print(host)
PY
)
((${#EXITS[@]} > 0)) || { echo "no enabled exits" >&2; exit 1; }
for exit_host in "${EXITS[@]}"; do
  echo "[e2e-all] testing $ENTRY through $exit_host" >&2
  "$SCRIPT_DIR/smoke-backend.sh" --inventory "$INVENTORY" --entry "$ENTRY" --exit "$exit_host"
done
echo "E2E ALL EXITS PASS: ${EXITS[*]}"
