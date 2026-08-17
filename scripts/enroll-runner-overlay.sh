#!/usr/bin/env bash
#
# Join a self-hosted GitHub Actions runner to the management WireGuard overlay.
#
# The runner reaches the management hub only through this overlay: Vault, the
# github-deploy SSH endpoint and every management address live there and nowhere
# on the public interface. Until the runner is a peer, control-deploy cannot
# talk to the hub at all.
#
# This runs ON THE RUNNER. It follows the same rule as every other machine in
# this system (roles/bootstrap_wireguard, roles/platform_wireguard): the private
# key is generated here and never leaves. Only the public key travels to the
# hub, printed at the end as a ready-to-run command for an operator to execute
# there. Nothing in this script needs, or is given, the hub's private key.
#
# The runner is a management-plane peer, not a traffic node, so it takes an
# address from the operator range of the environment network (…255.x), well
# clear of the node slots (…1.x / …2.x). The hub side is registered with the
# existing bounded peer command, whose validation this script mirrors so a bad
# value fails here rather than after a copy-paste to the hub.

set -euo pipefail
umask 077

usage() {
  cat >&2 <<'USAGE'
usage: enroll-runner-overlay.sh \
         --hub-public-key <base64>        # /etc/wireguard/wg0.pub on the hub
         --hub-endpoint <host:port>       # public address, e.g. 202.50.55.242:51820
         --hub-overlay-address <ipv4>     # the hub address the runner will SSH to, e.g. 10.80.0.1
         --runner-address <ipv4/32>       # a free operator-range /32, e.g. 10.80.255.240/32
         [--runner-id <id>]               # peer label on the hub (default: ci-runner)
         [--interface <name>]             # local WG interface (default: wg-spirit)

Run as root on the runner. Prints the single command to run on the hub.
USAGE
  exit 64
}

fail() {
  printf 'runner overlay enrollment failed: %s\n' "$*" >&2
  exit 1
}

hub_public_key=""
hub_endpoint=""
hub_overlay_address=""
runner_address=""
runner_id="ci-runner"
interface="wg-spirit"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hub-public-key) hub_public_key="${2:-}"; shift 2 ;;
    --hub-endpoint) hub_endpoint="${2:-}"; shift 2 ;;
    --hub-overlay-address) hub_overlay_address="${2:-}"; shift 2 ;;
    --runner-address) runner_address="${2:-}"; shift 2 ;;
    --runner-id) runner_id="${2:-}"; shift 2 ;;
    --interface) interface="${2:-}"; shift 2 ;;
    -h|--help) usage ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage ;;
  esac
done

[[ "$EUID" -eq 0 ]] || fail "root is required to write /etc/wireguard and bring the interface up"
[[ -n "$hub_public_key" && -n "$hub_endpoint" && -n "$hub_overlay_address" && -n "$runner_address" ]] || usage

# Same shapes the hub enforces, checked here so a typo fails before it reaches
# the hub. A WireGuard key is 32 bytes in base64: 43 characters and one '='.
[[ "$hub_public_key" =~ ^[A-Za-z0-9+/]{43}=$ ]] || fail "hub public key is not a WireGuard key"
[[ "$interface" =~ ^[A-Za-z0-9_.-]{1,15}$ ]] || fail "invalid interface name"
[[ "$runner_id" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || fail "runner id must match ^[a-z0-9][a-z0-9-]{0,62}$"
[[ "$hub_endpoint" =~ ^[0-9.]+:[0-9]{1,5}$ ]] || fail "hub endpoint must be host:port"
[[ "$hub_overlay_address" =~ ^[0-9.]+$ ]] || fail "hub overlay address must be a bare IPv4"
[[ "$runner_address" =~ ^[0-9.]+/32$ ]] || fail "runner address must be a single /32, e.g. 10.80.255.240/32"

# The overlay networks, mirrored from platform_wireguard defaults. The runner's
# address has to live inside one of them, and the hub address it targets has to
# be that same network's hub host (…0.1), or return traffic would never route.
python3 - "$runner_address" "$hub_overlay_address" <<'PY' || fail "runner address is not a valid operator-range /32 for the hub's network"
import ipaddress
import sys

networks = {
    "develop": ipaddress.ip_network("10.80.0.0/16"),
    "prod": ipaddress.ip_network("10.82.0.0/16"),
}
runner = ipaddress.ip_interface(sys.argv[1])
hub = ipaddress.ip_address(sys.argv[2])
if runner.version != 4 or runner.network.prefixlen != 32:
    raise SystemExit(1)
for network in networks.values():
    if runner.ip in network and hub in network:
        if runner.ip == network.network_address or runner.ip == (network.network_address + 1):
            raise SystemExit(1)  # never the network address or the hub itself
        break
else:
    raise SystemExit(1)  # runner and hub must share one environment network
PY

command -v wg >/dev/null 2>&1 && command -v wg-quick >/dev/null 2>&1 \
  || fail "wireguard-tools is missing; install it first (apt-get install -y wireguard-tools)"

config_path="/etc/wireguard/${interface}.conf"
if [[ -e "$config_path" ]]; then
  fail "$config_path already exists; remove it deliberately before re-enrolling"
fi

install -d -o root -g root -m 0700 /etc/wireguard

# The private key is created here and stays here. Only its public half is ever
# printed, and only to hand to the hub.
private_key="$(wg genkey)"
public_key="$(printf '%s' "$private_key" | wg pubkey)"

temporary="$(mktemp "${config_path}.tmp.XXXXXX")"
trap 'rm -f "$temporary"' EXIT
{
  printf '%s\n' '[Interface]'
  printf '# spiritvpn runner: %s\n' "$runner_id"
  printf 'Address = %s\n' "$runner_address"
  printf 'PrivateKey = %s\n' "$private_key"
  printf '\n'
  printf '%s\n' '[Peer]'
  printf '# management hub\n'
  printf 'PublicKey = %s\n' "$hub_public_key"
  printf 'Endpoint = %s\n' "$hub_endpoint"
  # Only the hub address the runner actually talks to is routed into the tunnel:
  # the overlay is a path to the hub, not a default route.
  printf 'AllowedIPs = %s/32\n' "$hub_overlay_address"
  # The hub learns the runner's public address from its handshakes rather than
  # from a static Endpoint, so the runner — very likely behind NAT — must keep
  # the mapping warm from its side.
  printf 'PersistentKeepalive = 25\n'
} > "$temporary"
install -o root -g root -m 0600 "$temporary" "$config_path"

systemctl enable --now "wg-quick@${interface}" >/dev/null 2>&1 \
  || fail "wg-quick@${interface} did not start; inspect: systemctl status wg-quick@${interface}"

runner_ip="${runner_address%/32}"
cat <<REPORT

Runner overlay interface ${interface} is up.
  runner address : ${runner_address}
  hub target     : ${hub_overlay_address} via ${hub_endpoint}
  public key     : ${public_key}

The handshake stays incomplete until the hub accepts this peer. Run ON THE HUB:

  sudo /usr/local/sbin/spiritvpn-wireguard-peer reconcile develop ${runner_id} ${runner_address} ${public_key}

Then verify from the runner:

  wg show ${interface} latest-handshakes
  ssh -o BatchMode=yes -i ~/.config/spiritvpn/keys/github-develop \\
      github-deploy@${hub_overlay_address} platform-readiness
REPORT
