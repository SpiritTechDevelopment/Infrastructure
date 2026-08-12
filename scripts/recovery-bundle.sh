#!/usr/bin/env bash
# Create a passphrase-encrypted recovery bundle of crown-jewel PRIVATE material,
# so a lost or dead laptop is survivable. The output
# recovery/<name>-recovery.age is SAFE TO COMMIT — it is encrypted under a
# passphrase you memorize (age's scrypt). The passphrase lives ONLY in your head;
# never write it into the repo. This script belongs to the isolated legacy contour.
set -Eeuo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="${1:-$(id -un)}"
OUT="$REPO/recovery/${NAME}-recovery.age"
command -v age >/dev/null || { echo "age is not installed" >&2; exit 2; }

# Crown-jewel private material. Only files that exist on this machine are bundled.
CANDIDATES=(
  "$HOME/.config/sops/age/keys.txt"       # decrypts every SOPS secret
  "$HOME/.ssh/spirit_ops"                 # SSH into the fleet
  "$HOME/spirit_wg.key"                   # WireGuard overlay identity
  "$REPO/.local-secrets/vault-init.json"  # Vault unseal keys + root token
)
files=()
for f in "${CANDIDATES[@]}"; do [[ -f "$f" ]] && files+=("$f"); done
[[ ${#files[@]} -gt 0 ]] || { echo "no recovery files found on this machine" >&2; exit 1; }

mkdir -p "$REPO/recovery"
echo "Bundling ${#files[@]} file(s). You will be prompted for a STRONG passphrase"
echo "(use a long diceware phrase and MEMORIZE it — it is the only way back in):"
printf '  %s\n' "${files[@]}"
echo

# GNU tar strips the leading '/'; restore with 'tar -x -C /'. age -p prompts twice.
tar -czf - "${files[@]}" 2>/dev/null | age -p -o "$OUT"
chmod 600 "$OUT"

echo
echo "Wrote $OUT (passphrase-encrypted — safe to commit)."
echo "Commit it:"
echo "  git add recovery/$(basename "$OUT") && git commit -m 'recovery: update ${NAME} bundle'"
