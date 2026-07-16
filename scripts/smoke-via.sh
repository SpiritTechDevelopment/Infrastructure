#!/usr/bin/env bash
# Compatibility wrapper: run the full backend/customer E2E against one selected exit.
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENTRY="entry-1"
EXIT_HOST=""
INVENTORY="$(cd "$SCRIPT_DIR/.." && pwd)/inventories/prod/inventory.yml"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --entry) ENTRY="$2"; shift 2 ;;
    --exit) EXIT_HOST="$2"; shift 2 ;;
    --inventory) INVENTORY="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 --exit HOST [--entry HOST] [--inventory PATH]"
      exit 0
      ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$EXIT_HOST" ]] || { echo "--exit is required" >&2; exit 2; }
exec "$SCRIPT_DIR/smoke-backend.sh" --inventory "$INVENTORY" --entry "$ENTRY" --exit "$EXIT_HOST"
