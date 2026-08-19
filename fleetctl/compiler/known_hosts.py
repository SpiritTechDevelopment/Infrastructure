"""SSH host identity projection for both connection phases.

This file used to be the one hand-maintained input left in the fleet path: a
root-owned `known_hosts` on the management hub that no role wrote and no
declaration described. Adding a node meant editing it by hand, and forgetting to
was invisible until the bootstrap failed.

It is a projection of desired state like everything else now. The host key is
declared on the instance, and the addresses come from the same two compilers the
inventories come from — so a node reached at an address is a node whose key was
declared for that address, by construction rather than by discipline.

Discovery is deliberately absent. `ssh-keyscan` asks the host being
authenticated to supply its own proof of identity, which answers the wrong
question: it tells you what is there, not what should be.
"""

from __future__ import annotations

from fleetctl.model import DesiredState

from .addressing import management_address


HEADER = (
    "# GENERATED — DO NOT EDIT\n"
    "# Rendered by fleetctl from desired state; edit the instance declaration instead.\n"
)

# Kept in step with the `ssh_host_key` pattern in the instance schema; the test
# suite asserts the two agree. More than one type is allowed because the fleet
# has more than one: the entry node was pinned by its ed25519 key and the
# hand-built exit node by RSA, and declaring what is true has to come before
# making it uniform. `ssh-rsa` names the key, not the SHA-1 signature algorithm
# it used to imply — a current client negotiates rsa-sha2-* over the same key.
HOST_KEY_TYPES = (
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "ssh-rsa",
)


def compile_known_hosts(state: DesiredState) -> str:
    nodes = {node.object_id: node for node in state.nodes}
    lines = [HEADER]
    for instance in sorted(state.instances, key=lambda item: item.object_id):
        # A retired instance is not reached by either inventory, and keeping its
        # key here would keep authorising a machine the fleet has let go.
        if instance.target_state == "retired":
            continue
        node = nodes[instance.logical_node]
        patterns = [
            # Bootstrap reaches a clean VPS on its public address, port 22.
            host_pattern(instance.public_address, 22),
            # Steady state reaches it over the management overlay on the
            # declared sshd port. Same machine, so the same host key.
            host_pattern(
                management_address(state.environment, node, instance),
                state.common_for_node(node.object_id).networking.ssh_port,
            ),
        ]
        lines.append(f"{','.join(dict.fromkeys(patterns))} {instance.ssh_host_key}\n")
    return "".join(lines)


def host_pattern(address: str, port: int) -> str:
    """Render one host pattern the way ssh looks it up.

    A non-default port makes ssh search for `[host]:port` and nothing else, so a
    plain address would silently never match. IPv6 literals take the same single
    pair of brackets; the address inside stays bare.
    """

    return address if port == 22 else f"[{address}]:{port}"
