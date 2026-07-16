#!/usr/bin/env bash
# Verify the tunnel egresses from the expected country. Point at a running client SOCKS.
# Usage: scripts/e2e.sh [socks_host:port] [expected_country_code]
set -euo pipefail
PROXY="${1:-127.0.0.1:10808}"
EXPECT="${2:-}"
echo "direct (no proxy) egress:"
curl -s https://api.ip.sb/geoip | python3 -c "import sys,json;d=json.load(sys.stdin);print(' ',d.get('ip'),d.get('country_code'),d.get('country'))" || true
echo "through tunnel ($PROXY):"
OUT=$(curl -s --socks5-hostname "$PROXY" https://api.ip.sb/geoip)
echo "$OUT" | python3 -c "import sys,json;d=json.load(sys.stdin);print(' ',d.get('ip'),d.get('country_code'),d.get('country'))"
if [ -n "$EXPECT" ]; then
  CC=$(echo "$OUT" | python3 -c "import sys,json;print(json.load(sys.stdin).get('country_code',''))")
  [ "$CC" = "$EXPECT" ] && echo "PASS: egress country = $EXPECT" || { echo "FAIL: got $CC, expected $EXPECT"; exit 1; }
fi
