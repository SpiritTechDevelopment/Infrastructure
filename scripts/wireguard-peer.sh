#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  wireguard-peer.sh NAME ADDRESS HUB_PUBLIC_KEY HUB_ENDPOINT MANAGEMENT_CIDR [DNS]

Example:
  wireguard-peer.sh controller 10.20.0.2/32 BASE64KEY 198.51.100.5:51820 10.20.0.0/24

Creates root-sensitive material under .local-secrets/wireguard/NAME and prints the
public-key inventory fragment to add under management_wireguard_external_peers.
USAGE
}

[[ $# -ge 5 && $# -le 6 ]] || { usage >&2; exit 2; }
command -v wg >/dev/null || { echo 'wg is required (wireguard-tools package)' >&2; exit 1; }

name=$1
address=$2
hub_public_key=$3
hub_endpoint=$4
management_cidr=$5
dns=${6:-}
out_dir=".local-secrets/wireguard/${name}"
if [[ -e "$out_dir/${name}.conf" ]]; then
  echo "Refusing to overwrite existing peer configuration: $out_dir/${name}.conf" >&2
  exit 1
fi
mkdir -p "$out_dir"
chmod 700 .local-secrets .local-secrets/wireguard "$out_dir" 2>/dev/null || true
umask 077

private_key=$(wg genkey)
public_key=$(printf '%s' "$private_key" | wg pubkey)

cat > "$out_dir/${name}.conf" <<CONF
[Interface]
Address = ${address}
PrivateKey = ${private_key}
${dns:+DNS = ${dns}}

[Peer]
PublicKey = ${hub_public_key}
Endpoint = ${hub_endpoint}
AllowedIPs = ${management_cidr}
PersistentKeepalive = 25
CONF
printf '%s\n' "$public_key" > "$out_dir/public.key"
chmod 600 "$out_dir/${name}.conf"
chmod 644 "$out_dir/public.key"

cat <<OUT
Created: $out_dir/${name}.conf
Public key: $public_key

Add this to the HUB host in inventories/prod/inventory.yml:

management_wireguard_external_peers:
  - name: "$name"
    public_key: "$public_key"
    allowed_ips:
      - "${address%/*}/32"

Then rerun: make management
Finally install the generated config on the external machine as /etc/wireguard/wg0.conf.
OUT
