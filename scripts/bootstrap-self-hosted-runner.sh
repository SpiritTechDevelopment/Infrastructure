#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage:
  bootstrap-self-hosted-runner.sh \
    --repository-url https://github.com/OWNER/REPOSITORY \
    --runner-version VERSION \
    --runner-sha256 SHA256 \
    [--runner-name NAME] \
    [--labels LABEL[,LABEL...]]

Run as root on a dedicated Debian or Ubuntu runner host. On the first run the
short-lived GitHub runner registration token is read from standard input. A
registered installation is left intact and only its systemd service is
re-asserted, so subsequent runs need no token.

The runner is installed as the unprivileged `github-runner` user under
/opt/actions-runner. The script intentionally does not grant sudo or Docker
access and does not configure cloud or management-host firewalls.
EOF
}

fail() {
  printf 'self-hosted runner bootstrap failed: %s\n' "$*" >&2
  exit 2
}

repository_url=""
runner_version=""
runner_sha256=""
runner_name="spiritvpn-deploy-1"
runner_labels="spiritvpn-deploy"
runner_user="github-runner"
runner_home="/var/lib/github-runner"
runner_dir="/opt/actions-runner"

while (($#)); do
  case "$1" in
    --repository-url)
      (($# >= 2)) || fail "--repository-url requires a value"
      repository_url="$2"
      shift 2
      ;;
    --runner-version)
      (($# >= 2)) || fail "--runner-version requires a value"
      runner_version="$2"
      shift 2
      ;;
    --runner-sha256)
      (($# >= 2)) || fail "--runner-sha256 requires a value"
      runner_sha256="${2,,}"
      shift 2
      ;;
    --runner-name)
      (($# >= 2)) || fail "--runner-name requires a value"
      runner_name="$2"
      shift 2
      ;;
    --labels)
      (($# >= 2)) || fail "--labels requires a value"
      runner_labels="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "unknown argument: $1"
      ;;
  esac
done

[[ $EUID -eq 0 ]] || fail "run this script as root"
[[ "$repository_url" =~ ^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/?$ ]] \
  || fail "--repository-url must be an https://github.com/OWNER/REPOSITORY URL"
[[ "$runner_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
  || fail "--runner-version must look like 2.123.4"
[[ "$runner_sha256" =~ ^[0-9a-f]{64}$ ]] \
  || fail "--runner-sha256 must contain 64 lowercase hexadecimal characters"
[[ "$runner_name" =~ ^[A-Za-z0-9._-]{1,64}$ ]] \
  || fail "--runner-name contains unsupported characters"
[[ "$runner_labels" =~ ^[A-Za-z0-9._-]+(,[A-Za-z0-9._-]+)*$ ]] \
  || fail "--labels must be a comma-separated list without spaces"

[[ -r /etc/os-release ]] || fail "/etc/os-release is unavailable"
# shellcheck disable=SC1091
source /etc/os-release
case "${ID:-}" in
  debian|ubuntu) ;;
  *) fail "only Debian and Ubuntu are supported, found ${ID:-unknown}" ;;
esac

case "$(uname -m)" in
  x86_64) runner_arch="x64" ;;
  aarch64|arm64) runner_arch="arm64" ;;
  *) fail "unsupported machine architecture: $(uname -m)" ;;
esac

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates \
  curl \
  git \
  jq \
  openssh-client \
  tar

if id "$runner_user" >/dev/null 2>&1; then
  configured_home="$(getent passwd "$runner_user" | cut -d: -f6)"
  [[ "$configured_home" == "$runner_home" ]] \
    || fail "$runner_user already exists with home $configured_home, expected $runner_home"
else
  useradd \
    --system \
    --user-group \
    --create-home \
    --home-dir "$runner_home" \
    --shell /usr/sbin/nologin \
    "$runner_user"
fi

runner_uid="$(id -u "$runner_user")"
[[ "$runner_uid" != 0 ]] || fail "$runner_user must not be root"
runner_groups=" $(id -nG "$runner_user") "
for privileged_group in sudo wheel docker lxd; do
  [[ "$runner_groups" != *" $privileged_group "* ]] \
    || fail "$runner_user must not belong to privileged group $privileged_group"
done

install -d -o "$runner_user" -g "$runner_user" -m 0750 "$runner_home" "$runner_dir"

if [[ ! -x "$runner_dir/config.sh" ]]; then
  if find "$runner_dir" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
    fail "$runner_dir is non-empty but does not contain config.sh"
  fi

  temporary_dir="$(mktemp -d /tmp/spiritvpn-actions-runner.XXXXXX)"
  cleanup() {
    rm -rf -- "$temporary_dir"
  }
  trap cleanup EXIT

  archive="$temporary_dir/actions-runner.tar.gz"
  archive_url="https://github.com/actions/runner/releases/download/v${runner_version}/actions-runner-linux-${runner_arch}-${runner_version}.tar.gz"
  printf 'Downloading GitHub Actions runner %s for linux-%s\n' "$runner_version" "$runner_arch"
  curl \
    --fail \
    --location \
    --proto '=https' \
    --tlsv1.2 \
    --retry 3 \
    --output "$archive" \
    "$archive_url"
  printf '%s  %s\n' "$runner_sha256" "$archive" | sha256sum --check --status \
    || fail "GitHub Actions runner archive checksum does not match"

  tar --extract --gzip --file "$archive" --directory "$runner_dir" --no-same-owner
  chown -R "$runner_user:$runner_user" "$runner_dir"
  "$runner_dir/bin/installdependencies.sh"
fi

if [[ -f "$runner_dir/.runner" ]]; then
  installed_repository="$(jq -r '.gitHubUrl // empty' "$runner_dir/.runner")"
  installed_name="$(jq -r '.agentName // empty' "$runner_dir/.runner")"
  [[ "${installed_repository%/}" == "${repository_url%/}" ]] \
    || fail "runner is already registered for ${installed_repository:-an unknown repository}"
  [[ "$installed_name" == "$runner_name" ]] \
    || fail "runner is already registered as ${installed_name:-an unknown name}"
  printf 'Runner %s is already registered for %s\n' "$runner_name" "$repository_url"
else
  registration_token=""
  if [[ -t 0 ]]; then
    printf 'GitHub runner registration token: ' >/dev/tty
    IFS= read -r -s registration_token </dev/tty
    printf '\n' >/dev/tty
  else
    IFS= read -r registration_token || true
  fi
  [[ -n "$registration_token" ]] || fail "registration token was not provided on standard input"

  (
    cd "$runner_dir"
    runuser -u "$runner_user" -- \
      env HOME="$runner_home" \
      ./config.sh \
        --unattended \
        --url "$repository_url" \
        --token "$registration_token" \
        --name "$runner_name" \
        --labels "$runner_labels" \
        --work _work
  )
  registration_token=""
fi

(
  cd "$runner_dir"
  if [[ ! -f .service ]]; then
    ./svc.sh install "$runner_user"
  fi
  ./svc.sh start
  ./svc.sh status
)

printf '\nRunner bootstrap complete.\n'
printf 'Repository: %s\n' "$repository_url"
printf 'Runner:     %s\n' "$runner_name"
printf 'Labels:     self-hosted,linux,%s,%s\n' "$runner_arch" "$runner_labels"
