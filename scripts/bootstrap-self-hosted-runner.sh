#!/usr/bin/env bash
# Reconcile or verify the dedicated GitHub runner from an exact-SHA private plan.
set -Eeuo pipefail
umask 077

usage() {
  cat >&2 <<'USAGE'
usage: bootstrap-self-hosted-runner.sh --plan <private-runner-host-plan.json> [--mode check|apply]

The plan must be generated from a clean committed checkout:

  python3 scripts/platform-sops.py runner-host-plan \
    --bundle inventories/bootstrap/platform.sops.yml \
    --source-git-sha "$(git rev-parse HEAD)" \
    --output /tmp/spiritvpn-runner-host-plan.json

Run this script as root on the runner. Check never changes the host. Apply may
install the declared bootstrap release and register a previously unregistered
runner; the short-lived registration token is read from standard input only
when registration is required. Existing registration is never replaced.
USAGE
  exit 64
}

fail() {
  printf 'self-hosted runner reconciliation failed: %s\n' "$*" >&2
  exit 2
}

plan=""
mode=check
while (($#)); do
  case "$1" in
    --plan)
      (($# >= 2)) || usage
      plan="$2"
      shift 2
      ;;
    --mode)
      (($# >= 2)) || usage
      mode="$2"
      shift 2
      ;;
    -h|--help) usage ;;
    *) usage ;;
  esac
done

[[ $EUID -eq 0 ]] || fail "run this script as root"
[[ "$mode" =~ ^(check|apply)$ ]] || usage
[[ -n "$plan" && -f "$plan" && ! -L "$plan" ]] || fail "plan must be a regular file"
plan_mode="$(stat -c '%a' "$plan")"
(( (8#$plan_mode & 8#077) == 0 )) || fail "plan must not be readable by group or others"
for command_name in python3 stat sha256sum getent id readlink awk cut; do
  command -v "$command_name" >/dev/null 2>&1 \
    || fail "required bootstrap command is unavailable: $command_name"
done

mapfile -d '' contract < <(
python3 - "$plan" <<'PY'
import json
import re
import sys
from pathlib import Path


def fail() -> None:
    raise SystemExit(64)


try:
    document = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    fail()
if not isinstance(document, dict) or set(document) != {
    "artifacts",
    "runner",
    "schema_version",
    "source_git_sha",
}:
    fail()
if document["schema_version"] != 1 or not re.fullmatch(
    r"[0-9a-f]{40}", str(document["source_git_sha"])
):
    fail()
artifacts = document["artifacts"]
if not isinstance(artifacts, dict) or set(artifacts) != {"bootstrap_script_sha256"}:
    fail()
script_sha256 = artifacts["bootstrap_script_sha256"]
if not isinstance(script_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", script_sha256):
    fail()
runner = document["runner"]
expected = {
    "architecture",
    "bootstrap_sha256",
    "bootstrap_version",
    "home",
    "install_dir",
    "labels",
    "name",
    "repository_url",
    "update_policy",
    "user",
    "work_dir",
}
if not isinstance(runner, dict) or set(runner) != expected:
    fail()
if runner["architecture"] not in {"x64", "arm64"}:
    fail()
if not isinstance(runner["bootstrap_version"], str) or not re.fullmatch(
    r"[0-9]+\.[0-9]+\.[0-9]+", runner["bootstrap_version"]
):
    fail()
if not isinstance(runner["bootstrap_sha256"], str) or not re.fullmatch(
    r"[0-9a-f]{64}", runner["bootstrap_sha256"]
):
    fail()
if not isinstance(runner["repository_url"], str) or not re.fullmatch(
    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/?",
    runner["repository_url"],
):
    fail()
if not isinstance(runner["name"], str) or not re.fullmatch(
    r"[A-Za-z0-9._-]{1,64}", runner["name"]
):
    fail()
labels = runner["labels"]
if (
    not isinstance(labels, list)
    or not labels
    or len(labels) != len(set(labels))
    or any(
        not isinstance(label, str)
        or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", label)
        for label in labels
    )
):
    fail()
if runner["update_policy"] != "github-managed":
    fail()
fixed_layout = {
    "user": "github-runner",
    "home": "/var/lib/github-runner",
    "install_dir": "/opt/actions-runner",
    "work_dir": "_work",
}
if any(runner[key] != value for key, value in fixed_layout.items()):
    fail()

values = (
    document["source_git_sha"],
    script_sha256,
    runner["repository_url"].rstrip("/"),
    runner["name"],
    runner["architecture"],
    runner["bootstrap_version"],
    runner["bootstrap_sha256"],
    ",".join(labels),
    runner["update_policy"],
    runner["user"],
    runner["home"],
    runner["install_dir"],
    runner["work_dir"],
)
for value in values:
    sys.stdout.buffer.write(value.encode("utf-8") + b"\0")
PY
)

[[ "${#contract[@]}" -eq 13 ]] || fail "Git-owned runner host plan is incomplete"
source_git_sha="${contract[0]}"
expected_script_sha256="${contract[1]}"
repository_url="${contract[2]}"
runner_name="${contract[3]}"
runner_arch="${contract[4]}"
bootstrap_version="${contract[5]}"
bootstrap_sha256="${contract[6]}"
runner_labels="${contract[7]}"
update_policy="${contract[8]}"
runner_user="${contract[9]}"
runner_home="${contract[10]}"
runner_dir="${contract[11]}"
runner_work_dir="${contract[12]}"

script_path="$(readlink -f "$0")"
actual_script_sha256="$(sha256sum "$script_path" | awk '{print $1}')"
[[ "$actual_script_sha256" == "$expected_script_sha256" ]] \
  || fail "bootstrap script does not belong to the plan's exact Git revision"

[[ -r /etc/os-release ]] || fail "/etc/os-release is unavailable"
# shellcheck disable=SC1091
source /etc/os-release
case "${ID:-}" in
  debian|ubuntu) ;;
  *) fail "only Debian and Ubuntu are supported, found ${ID:-unknown}" ;;
esac

case "$(uname -m)" in
  x86_64) actual_arch=x64 ;;
  aarch64|arm64) actual_arch=arm64 ;;
  *) fail "unsupported machine architecture: $(uname -m)" ;;
esac
[[ "$actual_arch" == "$runner_arch" ]] \
  || fail "host architecture differs from the Git-owned runner plan"

check_runner() {
  for command_name in jq runuser systemctl; do
    command -v "$command_name" >/dev/null 2>&1 \
      || fail "required runner command is unavailable: $command_name"
  done
  local passwd_entry configured_home configured_shell runner_groups installed_version
  local installed_repository installed_name installed_work service_name disable_update
  passwd_entry="$(getent passwd "$runner_user")" \
    || fail "declared runner account is absent"
  configured_home="$(printf '%s' "$passwd_entry" | cut -d: -f6)"
  configured_shell="$(printf '%s' "$passwd_entry" | cut -d: -f7)"
  [[ "$configured_home" == "$runner_home" ]] \
    || fail "runner account home differs from the Git-owned plan"
  [[ "$configured_shell" == /usr/sbin/nologin ]] \
    || fail "runner account must use /usr/sbin/nologin"
  [[ "$(stat -c '%U:%G:%a' "$runner_home")" == "$runner_user:$runner_user:750" ]] \
    || fail "runner home ownership or mode drifted"
  [[ "$(stat -c '%U:%G:%a' "$runner_dir")" == "$runner_user:$runner_user:750" ]] \
    || fail "runner installation directory ownership or mode drifted"
  runner_groups=" $(id -nG "$runner_user") "
  for privileged_group in sudo wheel docker lxd; do
    [[ "$runner_groups" != *" $privileged_group "* ]] \
      || fail "runner account belongs to privileged group $privileged_group"
  done
  [[ -x "$runner_dir/config.sh" && -x "$runner_dir/bin/Runner.Listener" ]] \
    || fail "runner software is incomplete"
  installed_version="$(runuser -u "$runner_user" -- "$runner_dir/bin/Runner.Listener" --version)"
  python3 - "$installed_version" "$bootstrap_version" <<'PY' \
    || fail "installed runner is older than the Git-owned bootstrap floor"
import re
import sys

if not all(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value) for value in sys.argv[1:]):
    raise SystemExit(1)
if tuple(map(int, sys.argv[1].split("."))) < tuple(map(int, sys.argv[2].split("."))):
    raise SystemExit(1)
PY
  [[ -f "$runner_dir/.runner" && ! -L "$runner_dir/.runner" ]] \
    || fail "runner is not registered"
  installed_repository="$(jq -r '.gitHubUrl // empty' "$runner_dir/.runner")"
  installed_name="$(jq -r '.agentName // empty' "$runner_dir/.runner")"
  installed_work="$(jq -r '.workFolder // empty' "$runner_dir/.runner")"
  disable_update="$(jq -r '.disableUpdate // false' "$runner_dir/.runner")"
  [[ "${installed_repository%/}" == "$repository_url" ]] \
    || fail "runner is registered for a different repository"
  [[ "$installed_name" == "$runner_name" ]] \
    || fail "runner is registered with a different name"
  [[ "$installed_work" == "$runner_work_dir" ]] \
    || fail "runner work directory differs from the Git-owned plan"
  [[ "$update_policy" == github-managed && "$disable_update" == false ]] \
    || fail "runner update policy differs from the Git-owned plan"
  [[ -f "$runner_dir/.service" && ! -L "$runner_dir/.service" ]] \
    || fail "runner systemd service marker is absent"
  service_name="$(<"$runner_dir/.service")"
  [[ "$service_name" =~ ^actions\.runner\.[A-Za-z0-9_.-]+\.service$ ]] \
    || fail "runner systemd service name is invalid"
  systemctl is-enabled --quiet "$service_name" \
    || fail "runner systemd service is not enabled"
  systemctl is-active --quiet "$service_name" \
    || fail "runner systemd service is not active"
  printf 'runner host %s: match (%s, installed version %s, source %s)\n' \
    "$runner_name" "$mode" "$installed_version" "$source_git_sha"
  printf 'expected_repository_labels=%s\n' "$runner_labels"
}

if [[ "$mode" == check ]]; then
  check_runner
  exit 0
fi

for command_name in apt-get curl tar useradd install find grep; do
  command -v "$command_name" >/dev/null 2>&1 \
    || fail "required apply command is unavailable: $command_name"
done
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates \
  curl \
  git \
  jq \
  openssh-client \
  python3 \
  tar

if id "$runner_user" >/dev/null 2>&1; then
  configured_home="$(getent passwd "$runner_user" | cut -d: -f6)"
  [[ "$configured_home" == "$runner_home" ]] \
    || fail "$runner_user already exists with an unexpected home"
else
  useradd \
    --system \
    --user-group \
    --create-home \
    --home-dir "$runner_home" \
    --shell /usr/sbin/nologin \
    "$runner_user"
fi
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
  archive_url="https://github.com/actions/runner/releases/download/v${bootstrap_version}/actions-runner-linux-${runner_arch}-${bootstrap_version}.tar.gz"
  curl \
    --fail \
    --location \
    --proto '=https' \
    --tlsv1.2 \
    --retry 3 \
    --output "$archive" \
    "$archive_url"
  printf '%s  %s\n' "$bootstrap_sha256" "$archive" | sha256sum --check --status \
    || fail "GitHub runner archive checksum does not match"
  tar --extract --gzip --file "$archive" --directory "$runner_dir" --no-same-owner
  chown -R "$runner_user:$runner_user" "$runner_dir"
  "$runner_dir/bin/installdependencies.sh"
fi

if [[ ! -f "$runner_dir/.runner" ]]; then
  registration_token=""
  if [[ -t 0 ]]; then
    printf 'GitHub runner registration token: ' >/dev/tty
    IFS= read -r -s registration_token </dev/tty
    printf '\n' >/dev/tty
  else
    IFS= read -r registration_token || true
  fi
  [[ -n "$registration_token" ]] \
    || fail "registration token was not provided on standard input"
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
        --work "$runner_work_dir"
  )
  registration_token=""
fi

(
  cd "$runner_dir"
  if [[ ! -f .service ]]; then
    ./svc.sh install "$runner_user"
  fi
  ./svc.sh start
)
check_runner
