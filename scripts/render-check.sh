#!/usr/bin/env bash
# Fastest inner loop: render each node's xray config and validate it. No servers touched.
set -euo pipefail
cd "$(dirname "$0")/.."
TARGET="${1:-entry:exit}"
INVENTORY="${ANSIBLE_INVENTORY:-examples/inventory.yml}"
rm -rf _render

echo "== syntax check =="
ansible-playbook -i "$INVENTORY" playbooks/fleet-infra.yml --syntax-check

echo "== render configs for: $TARGET =="
ansible-playbook -i "$INVENTORY" playbooks/render-check.yml -e "target=$TARGET" -e ansible_become=false

echo "== validate rendered JSON =="
shopt -s nullglob
rendered=(_render/*.json)
((${#rendered[@]} > 0)) || { echo "no Xray configurations were rendered" >&2; exit 1; }
for f in "${rendered[@]}"; do
  python3 -c "import json,sys; json.load(open('$f')); print('  JSON OK:', '$f')"
done

if command -v docker >/dev/null 2>&1; then
  echo "== xray -test each rendered config (deep validation) =="
  for f in "${rendered[@]}"; do
    # Image ENTRYPOINT is already `xray`; do not pass another `xray` token.
    docker run --rm --user 0:0 -v "$PWD/$f":/c.json:ro "${XRAY_IMAGE:-ghcr.io/xtls/xray-core:26.3.27}" \
      run -test -config /c.json && echo "  xray OK: $f"
  done
else
  echo "(docker not found — skipped xray -test; JSON validity still checked)"
fi
