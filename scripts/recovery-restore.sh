#!/usr/bin/env bash
# Restore a passphrase-encrypted recovery bundle (created by recovery-bundle.sh)
# onto a fresh machine after a laptop loss. See RECOVERY.md.
#
# Usage: recovery-restore.sh recovery/<name>-recovery.age [DEST]
#   DEST defaults to /  (files restore to their original absolute paths).
#   Pass a staging dir (e.g. ./restore-review) to inspect before placing them.
set -Eeuo pipefail

BUNDLE="${1:?usage: recovery-restore.sh recovery/<name>-recovery.age [DEST]}"
DEST="${2:-/}"
command -v age >/dev/null || { echo "age is not installed" >&2; exit 2; }
[[ -f "$BUNDLE" ]] || { echo "no such bundle: $BUNDLE" >&2; exit 1; }

echo "Decrypting $BUNDLE (enter your recovery passphrase)."
echo "Extracting into: $DEST"
[[ "$DEST" == "/" ]] && echo "  (restores to original paths; assumes the same username/home)"
mkdir -p "$DEST"
age -d "$BUNDLE" | tar -xzvf - -C "$DEST"

echo
echo "Done. Sanity check the crown jewels:"
echo "  ls -l ~/.config/sops/age/keys.txt ~/.ssh/spirit_ops 2>/dev/null"
echo "Then: sops -d inventories/prod/secrets.sops.yml >/dev/null && echo 'age key OK'"
