#!/usr/bin/env bash
# Install pinned SOPS/age binaries and create a runner-local age identity.
set -euo pipefail
umask 077

sops_version=3.13.3
sops_sha256=e5bec3346a873ae91d871550f3e698c1aad962aff462a080e40f25fde17fef6b
age_version=1.3.1
age_sha256=bdc69c09cbdd6cf8b1f333d372a1f58247b3a33146406333e30c0f26e8f51377

fail() {
  printf 'runner SOPS bootstrap failed: %s\n' "$*" >&2
  exit 2
}

[[ "$(id -un)" == github-runner ]] || fail "must run as github-runner"
[[ "$(uname -s)" == Linux ]] || fail "only Linux is supported"
[[ "$(uname -m)" == x86_64 ]] || fail "only x86_64 is currently pinned"
command -v curl >/dev/null || fail "curl is required"
command -v tar >/dev/null || fail "tar is required"

runner_home="${HOME:?HOME is required}"
case "$runner_home" in
  /var/lib/github-runner|/home/github-runner) ;;
  *) fail "unexpected github-runner home: $runner_home" ;;
esac

bin_dir="$runner_home/.local/spiritvpn/bin"
identity_dir="$runner_home/.config/spiritvpn/sops"
identity_file="$identity_dir/age-identity.txt"
recipient_file="$identity_dir/age-recipient.txt"
install -d -m 0700 "$bin_dir" "$identity_dir"

temporary="$(mktemp -d "${RUNNER_TEMP:-/tmp}/spiritvpn-runner-sops.XXXXXXXX")"
cleanup() {
  rm -rf -- "$temporary"
}
trap cleanup EXIT

sops_download="$temporary/sops"
curl --fail --location --proto '=https' --tlsv1.2 --retry 3 \
  --output "$sops_download" \
  "https://github.com/getsops/sops/releases/download/v${sops_version}/sops-v${sops_version}.linux.amd64"
printf '%s  %s\n' "$sops_sha256" "$sops_download" | sha256sum --check --status \
  || fail "SOPS checksum does not match"
install -m 0755 "$sops_download" "$bin_dir/sops"

age_archive="$temporary/age.tar.gz"
curl --fail --location --proto '=https' --tlsv1.2 --retry 3 \
  --output "$age_archive" \
  "https://github.com/FiloSottile/age/releases/download/v${age_version}/age-v${age_version}-linux-amd64.tar.gz"
printf '%s  %s\n' "$age_sha256" "$age_archive" | sha256sum --check --status \
  || fail "age checksum does not match"
tar --extract --gzip --file "$age_archive" --directory "$temporary"
test -x "$temporary/age/age-keygen" || fail "age archive has an unexpected layout"
install -m 0755 "$temporary/age/age" "$temporary/age/age-keygen" "$bin_dir/"

if [[ ! -e "$identity_file" ]]; then
  "$bin_dir/age-keygen" -o "$identity_file" >/dev/null
fi
test -f "$identity_file" && test ! -L "$identity_file" \
  || fail "identity must be a regular file"
chmod 0600 "$identity_file"
"$bin_dir/age-keygen" -y "$identity_file" > "$recipient_file"
chmod 0600 "$recipient_file"
"$bin_dir/age-keygen" -y "$identity_file" | cmp -s - "$recipient_file" \
  || fail "recipient does not match identity"

SOPS_DISABLE_VERSION_CHECK=1 "$bin_dir/sops" --version >/dev/null
printf 'runner_sops_path=%s\n' "$bin_dir/sops"
printf 'runner_age_identity=%s\n' "$identity_file"
printf 'runner_age_recipient='
cat "$recipient_file"
