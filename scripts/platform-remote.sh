#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  printf '%s\n' 'usage: platform-remote.sh <management-host> platform-readiness' >&2
  exit 64
fi

host="$1"
operation="$2"
if [[ ! "$host" =~ ^[A-Za-z0-9.:[\]-]+$ ]]; then
  printf '%s\n' 'invalid management host' >&2
  exit 64
fi
case "$operation" in
  platform-readiness) ;;
  *)
    printf '%s\n' 'unsupported platform operation' >&2
    exit 64
    ;;
esac

: "${PLATFORM_SSH_PRIVATE_KEY_FILE:?PLATFORM_SSH_PRIVATE_KEY_FILE is required}"
: "${PLATFORM_SSH_KNOWN_HOSTS_FILE:?PLATFORM_SSH_KNOWN_HOSTS_FILE is required}"

exec ssh \
  -F /dev/null \
  -i "$PLATFORM_SSH_PRIVATE_KEY_FILE" \
  -o IdentitiesOnly=yes \
  -o BatchMode=yes \
  -o StrictHostKeyChecking=yes \
  -o "UserKnownHostsFile=$PLATFORM_SSH_KNOWN_HOSTS_FILE" \
  -o PasswordAuthentication=no \
  -o KbdInteractiveAuthentication=no \
  -o RequestTTY=no \
  -- "github-deploy@$host" "$operation"
