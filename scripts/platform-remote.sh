#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  printf '%s\n' 'usage: platform-remote.sh <management-host> <platform-readiness|platform-deploy ...|fleet-deploy ...>' >&2
  exit 64
fi

host="$1"
operation="$2"
shift 2
if [[ ! "$host" =~ ^[A-Za-z0-9.:[\]-]+$ ]]; then
  printf '%s\n' 'invalid management host' >&2
  exit 64
fi
case "$operation" in
  platform-readiness)
    [[ $# -eq 0 ]] || { printf '%s\n' 'platform-readiness accepts no arguments' >&2; exit 64; }
    remote_command=platform-readiness
    ;;
  platform-deploy)
    [[ $# -eq 3 ]] || { printf '%s\n' 'platform-deploy requires environment, SHA and mode' >&2; exit 64; }
    environment="$1"
    source_git_sha="$2"
    mode="$3"
    [[ "$environment" =~ ^(develop|staging|prod)$ ]] || { printf '%s\n' 'invalid environment' >&2; exit 64; }
    [[ "$source_git_sha" =~ ^[0-9a-f]{40}$ ]] || { printf '%s\n' 'invalid source Git SHA' >&2; exit 64; }
    [[ "$mode" =~ ^(check|apply)$ ]] || { printf '%s\n' 'invalid platform deployment mode' >&2; exit 64; }
    remote_command="platform-deploy $environment $source_git_sha $mode"
    ;;
  fleet-deploy)
    [[ $# -eq 6 ]] || { printf '%s\n' 'fleet-deploy requires environment, SHA, mode and three boolean flags' >&2; exit 64; }
    environment="$1"
    source_git_sha="$2"
    mode="$3"
    initial="$4"
    resume="$5"
    allow_destructive="$6"
    [[ "$environment" =~ ^(develop|staging|prod)$ ]] || { printf '%s\n' 'invalid environment' >&2; exit 64; }
    [[ "$source_git_sha" =~ ^[0-9a-f]{40}$ ]] || { printf '%s\n' 'invalid source Git SHA' >&2; exit 64; }
    [[ "$mode" =~ ^(dry-run|apply)$ ]] || { printf '%s\n' 'invalid deployment mode' >&2; exit 64; }
    [[ "$initial" =~ ^(true|false)$ ]] || { printf '%s\n' 'invalid initial flag' >&2; exit 64; }
    [[ "$resume" =~ ^(true|false)$ ]] || { printf '%s\n' 'invalid resume flag' >&2; exit 64; }
    [[ "$allow_destructive" =~ ^(true|false)$ ]] || { printf '%s\n' 'invalid destructive flag' >&2; exit 64; }
    remote_command="fleet-deploy $environment $source_git_sha $mode $initial $resume $allow_destructive"
    ;;
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
  -- "github-deploy@$host" "$remote_command"
