"""Cross-object desired-state invariants that JSON Schema cannot express."""

from __future__ import annotations

import ipaddress
import re
from collections import Counter, defaultdict

from fleetctl.model import (
    CommonConfig,
    ControlPlane,
    DesiredState,
    Environment,
    Fleet,
    Instance,
    LogicalNode,
)

from .issues import ValidationIssue


EXPECTED_NETWORKS = {
    "develop": "10.80.0.0/16",
    "prod": "10.82.0.0/16",
}


def validate_semantics(state: DesiredState) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    environment = state.environment
    env = environment.object_id
    _validate_environment(environment, issues)
    _validate_bot(state, issues)
    _validate_common(state, issues)
    _validate_identifiers(state, issues)
    _validate_fleet_ids(state, issues)
    _validate_instances(state, issues)
    _validate_fleets(state, issues)
    _validate_secrets_and_public_identity(state, issues)
    if len(state.fleets) > 100:
        issues.append(ValidationIssue.at(environment.source, "LIMIT_FLEETS", "environment contains more than 100 fleets"))
    if len(state.nodes) > 100:
        issues.append(ValidationIssue.at(environment.source, "LIMIT_NODES", "environment contains more than 100 logical nodes"))
    if sum(len(fleet.bridges) for fleet in state.fleets) > 900:
        issues.append(ValidationIssue.at(environment.source, "LIMIT_BRIDGES", "environment contains more than 900 bridges"))
    return issues


def _validate_common(state: DesiredState, issues: list[ValidationIssue]) -> None:
    _validate_common_policy(state.environment_common, issues)
    for node in state.nodes:
        common = state.common_for_node(node.object_id)
        _validate_required_components(common, issues, require_digests=True)
        if common is not state.environment_common:
            _validate_common_policy(common, issues, validate_components=False)


def _validate_required_components(
    common: CommonConfig,
    issues: list[ValidationIssue],
    *,
    require_digests: bool,
) -> None:
    required_node_components = {"xray", "nginx_mask", "alloy", "node_agent", "node_exporter"}
    missing_components = sorted(required_node_components - set(common.components.components))
    if missing_components:
        issues.append(
            ValidationIssue.at(
                common.components.source,
                "COMMON_COMPONENT_MISSING",
                f"missing required traffic-node components: {', '.join(missing_components)}",
            )
        )
    if require_digests:
        for name in sorted(required_node_components & set(common.components.components)):
            if common.components.components[name].digest is None:
                issues.append(
                    ValidationIssue.at(
                        common.components.source,
                        "COMPONENT_DIGEST_REQUIRED",
                        f"component {name!r} needs an immutable digest before traffic nodes are declared",
                    )
                )


def _validate_common_policy(
    common: CommonConfig,
    issues: list[ValidationIssue],
    *,
    validate_components: bool = True,
) -> None:
    if validate_components:
        _validate_required_components(common, issues, require_digests=False)
    if common.networking.dns_ttl_seconds != 60 or common.networking.dns_proxied:
        issues.append(
            ValidationIssue.at(
                common.networking.source,
                "DNS_POLICY",
                "traffic-node DNS records must use ttl_seconds=60 and proxied=false",
            )
        )
    if common.observability.probe_timeout_seconds >= common.observability.probe_interval_seconds:
        issues.append(
            ValidationIssue.at(
                common.observability.source,
                "PROBE_INTERVAL",
                "probe timeout must be shorter than probe interval",
            )
        )
    if common.observability.activity_retention_days > common.observability.operations_retention_days:
        issues.append(
            ValidationIssue.at(
                common.observability.source,
                "RETENTION_POLICY",
                "activity retention cannot exceed operations retention",
            )
        )
    if common.rollout.max_parallel_logical_nodes_per_fleet != 1:
        issues.append(
            ValidationIssue.at(
                common.rollout.source,
                "ROLLOUT_PARALLELISM",
                "rollout must change at most one logical node per fleet at a time",
            )
        )
    if common.xray.default_outbound_tag != common.xray.block_outbound_tag:
        issues.append(
            ValidationIssue.at(
                common.xray.source,
                "XRAY_DEFAULT_OUTBOUND",
                "default outbound must be the fail-closed block outbound",
            )
        )
    # There was a rule here requiring the access log to be enabled "for local
    # accounting classification". Its consumer does not exist: NODE_AGENT_SPEC
    # §14 puts the activity subsystem out of scope for v1 and states the agent
    # does not read the Xray access log at all, while per-user volume comes
    # from the stats API instead. The rule therefore mandated writing client
    # addresses and destinations for a feature that was never built, so
    # `enabled` is now a choice desired state records rather than a constant.
    #
    # The export prohibition below stays: it is the half that still means
    # something, and it keeps the log from ever becoming a central archive.
    if common.xray.access_log_export_enabled:
        issues.append(
            ValidationIssue.at(
                common.xray.source,
                "ACCESS_LOG_EXPORT",
                "Xray access-log export must remain disabled",
            )
        )


def _validate_control_public_endpoint(
    environment: Environment,
    control: ControlPlane,
    issues: list[ValidationIssue],
) -> None:
    """Публичная точка хаба: объявлена целиком, адресом и внутри своей зоны.

    Хаб один на оба окружения, поэтому объявить его дважды с разными адресами
    можно только ошибкой. Поймать её здесь нельзя: валидация видит ровно одно
    окружение. Пока `prod` не объявляет `control`, конфликту неоткуда взяться;
    когда объявит — правилу понадобится межокруженческий шов, которого сейчас
    в `fleetctl` нет.
    """

    hostname = control.public_hostname
    address = control.public_address
    if hostname is None and address is None:
        return
    if hostname is None or address is None:
        issues.append(
            ValidationIssue.at(
                environment.source,
                "CONTROL_PUBLIC_ENDPOINT",
                "control public_endpoint must declare both hostname and address",
            )
        )
        return
    try:
        ipaddress.ip_address(address)
    except ValueError:
        issues.append(
            ValidationIssue.at(
                environment.source,
                "CONTROL_PUBLIC_ENDPOINT",
                f"control public_endpoint address {address!r} is not an IP address",
            )
        )
    zone = environment.dns_zone
    if hostname != zone and not hostname.endswith(f".{zone}"):
        issues.append(
            ValidationIssue.at(
                environment.source,
                "CONTROL_PUBLIC_ENDPOINT",
                f"control public_endpoint hostname {hostname!r} is outside zone {zone}",
            )
        )


def _validate_environment(environment: Environment, issues: list[ValidationIssue]) -> None:
    env = environment.object_id
    expected_network = EXPECTED_NETWORKS[env]
    if environment.management_network != expected_network:
        issues.append(
            ValidationIssue.at(
                environment.source,
                "ENV_NETWORK",
                f"{env} management_network must be {expected_network}",
            )
        )
    if environment.secret_kv != f"kv/{env}" or environment.secret_pki != f"pki/{env}":
        issues.append(
            ValidationIssue.at(
                environment.source,
                "ENV_SECRET_PATH",
                f"secret_store paths must belong to environment {env}",
            )
        )
    control = environment.control
    if control is None:
        return
    _validate_control_public_endpoint(environment, control, issues)
    if control.postgres_owner_user == control.postgres_runtime_user:
        issues.append(
            ValidationIssue.at(
                environment.source,
                "CONTROL_DB_ROLES",
                "control PostgreSQL owner and runtime users must be distinct",
            )
        )
    if env == "prod" and not control.backup_required:
        issues.append(
            ValidationIssue.at(
                environment.source,
                "CONTROL_BACKUP_REQUIRED",
                "prod control deployment must require a pre-migration backup",
            )
        )
    backup_command = control.external_backup_command_argv
    if backup_command and (
        not backup_command[0].startswith("/")
        or any(argument != argument.strip() or "\n" in argument for argument in backup_command)
    ):
        issues.append(
            ValidationIssue.at(
                environment.source,
                "CONTROL_BACKUP_COMMAND",
                "external backup argv must start with an absolute executable and contain "
                "only trimmed single-line arguments",
            )
        )
    if control.backup_required and not backup_command:
        issues.append(
            ValidationIssue.at(
                environment.source,
                "CONTROL_BACKUP_COMMAND",
                "a required control backup must declare an external backup command",
            )
        )
    for identity in (*control.customer_access_writers, *control.customer_access_readers):
        if "," in identity or identity != identity.strip():
            issues.append(
                ValidationIssue.at(
                    environment.source,
                    "CONTROL_IDENTITY",
                    "control client identities must be trimmed and must not contain commas",
                )
            )


def _validate_bot(state: DesiredState, issues: list[ValidationIssue]) -> None:
    """The bot is a tenant of the control host, and this is the rent.

    It shares one PostgreSQL instance with the backend and reaches it as an
    ordinary mTLS client. Both of those are places where a plausible-looking
    desired state produces silent damage rather than a failure: a repeated
    database name lets bot migrations run against backend tables, and an
    identity the backend does not authorise leaves a bot that starts, connects
    and is refused on every call.
    """
    environment = state.environment
    control = environment.control
    if control is None or control.bot is None:
        return
    bot = control.bot
    source = environment.source

    if bot.postgres_database == control.postgres_database:
        issues.append(
            ValidationIssue.at(
                source,
                "BOT_DB_SHARED",
                "bot database must not be the backend database",
            )
        )
    roles = (bot.postgres_owner_user, bot.postgres_runtime_user)
    if bot.postgres_owner_user == bot.postgres_runtime_user:
        issues.append(
            ValidationIssue.at(
                source,
                "BOT_DB_ROLES",
                "bot PostgreSQL owner and runtime users must be distinct",
            )
        )
    if set(roles) & {control.postgres_owner_user, control.postgres_runtime_user}:
        issues.append(
            ValidationIssue.at(
                source,
                "BOT_DB_ROLE_SHARED",
                "bot PostgreSQL roles must not reuse the backend roles",
            )
        )
    if bot.friends_plan_fleet not in state.fleet_ids:
        issues.append(
            ValidationIssue.at(
                source,
                "BOT_FLEET_UNKNOWN",
                f"bot friends_plan_fleet {bot.friends_plan_fleet!r} has no vpn_fleet_id",
            )
        )
    # Writer and reader both: the bot issues access and then reads back the
    # VLESS URI it hands the customer. Authorised for one only is a bot that
    # half-works, which is worse than one that does not start.
    if bot.client_identity not in control.customer_access_writers:
        issues.append(
            ValidationIssue.at(
                source,
                "BOT_IDENTITY_UNAUTHORISED",
                "bot client identity is not a customer_access_writer",
            )
        )
    if bot.client_identity not in control.customer_access_readers:
        issues.append(
            ValidationIssue.at(
                source,
                "BOT_IDENTITY_UNAUTHORISED",
                "bot client identity is not a customer_access_reader",
            )
        )


def _validate_identifiers(state: DesiredState, issues: list[ValidationIssue]) -> None:
    env = state.environment.object_id
    all_objects: tuple[Environment | Fleet | LogicalNode | Instance, ...] = (
        state.environment,
        *state.fleets,
        *state.nodes,
        *state.instances,
    )
    by_id: dict[str, list[Environment | Fleet | LogicalNode | Instance]] = defaultdict(list)
    for item in all_objects:
        by_id[item.object_id].append(item)
    for object_id, items in by_id.items():
        if len(items) > 1:
            for item in items:
                issues.append(ValidationIssue.at(item.source, "DUPLICATE_ID", f"identifier {object_id!r} is not globally unique"))

    for fleet in state.fleets:
        if not fleet.object_id.startswith(f"{env}-fleet-"):
            issues.append(ValidationIssue.at(fleet.source, "FLEET_NAME", f"fleet ID must start with {env}-fleet-"))
    for node in state.nodes:
        if not node.object_id.startswith(f"{env}-{node.role}-"):
            issues.append(
                ValidationIssue.at(node.source, "NODE_NAME", f"node ID must start with {env}-{node.role}-")
            )
    for instance in state.instances:
        if not instance.object_id.startswith(f"{env}-"):
            issues.append(ValidationIssue.at(instance.source, "INSTANCE_ENV", f"instance ID must include environment {env}"))


def _validate_fleet_ids(state: DesiredState, issues: list[ValidationIssue]) -> None:
    env = state.environment.object_id
    used_values: dict[int, str] = {}
    for fleet in state.fleets:
        value = state.fleet_ids.get(fleet.object_id)
        if value is None:
            issues.append(ValidationIssue.at(fleet.source, "FLEET_ID_MISSING", "fleet is absent from desired/fleet-ids.yml"))
            continue
        previous = used_values.get(value)
        if previous is not None:
            issues.append(
                ValidationIssue.at(
                    fleet.source,
                    "FLEET_ID_DUPLICATE",
                    f"vpn_fleet_id {value} is already used by {previous!r} in {env}",
                )
            )
        used_values[value] = fleet.object_id


def _validate_instances(state: DesiredState, issues: list[ValidationIssue]) -> None:
    nodes = {node.object_id: node for node in state.nodes}
    serving = Counter(instance.logical_node for instance in state.instances if instance.target_state == "serving")
    slots: dict[tuple[str, int], Instance] = {}
    for instance in state.instances:
        node = nodes.get(instance.logical_node)
        if node is None:
            issues.append(
                ValidationIssue.at(instance.source, "INSTANCE_NODE", f"logical node {instance.logical_node!r} does not exist")
            )
            continue
        common = state.common_for_node(node.object_id)
        if instance.bandwidth_profile not in common.limits.bandwidth_profiles:
            issues.append(
                ValidationIssue.at(
                    instance.source,
                    "BANDWIDTH_PROFILE",
                    f"bandwidth profile {instance.bandwidth_profile!r} does not exist in the node's effective limits",
                )
            )
        expected_pattern = rf"^{re.escape(node.object_id)}-[0-9]{{2,3}}$"
        if re.fullmatch(expected_pattern, instance.object_id) is None:
            issues.append(ValidationIssue.at(instance.source, "INSTANCE_NAME", f"instance ID must match {node.object_id}-<slot>"))
        else:
            suffix = instance.object_id.rsplit("-", 1)[1]
            slot = int(suffix)
            if not 1 <= slot <= 240 or suffix != f"{slot:02d}":
                issues.append(ValidationIssue.at(instance.source, "INSTANCE_SLOT", "management slot must be canonical 01 through 240"))
            else:
                key = (node.role, slot)
                previous = slots.get(key)
                if previous is not None:
                    issues.append(
                        ValidationIssue.at(
                            instance.source,
                            "MANAGEMENT_COLLISION",
                            f"management slot {slot} for role {node.role} is already used by {previous.object_id!r}",
                        )
                    )
                slots[key] = instance
    for node in state.nodes:
        count = serving[node.object_id]
        if count != 1:
            issues.append(
                ValidationIssue.at(node.source, "SERVING_COUNT", f"logical node must have exactly one serving instance, found {count}")
            )


def _validate_fleets(state: DesiredState, issues: list[ValidationIssue]) -> None:
    env = state.environment.object_id
    nodes = {node.object_id: node for node in state.nodes}
    membership: Counter[str] = Counter()
    for fleet in state.fleets:
        node_ids = (*fleet.entries, *fleet.exits)
        if len(node_ids) > 10:
            issues.append(ValidationIssue.at(fleet.source, "FLEET_NODE_LIMIT", "fleet contains more than 10 nodes"))
        if set(fleet.entries) & set(fleet.exits):
            issues.append(ValidationIssue.at(fleet.source, "FLEET_ROLE_OVERLAP", "a node cannot be both entry and exit"))
        membership.update(node_ids)
        role_members = tuple((item, "entry") for item in fleet.entries) + tuple(
            (item, "exit") for item in fleet.exits
        )
        for node_id, expected_role in role_members:
            node = nodes.get(node_id)
            if node is None:
                issues.append(ValidationIssue.at(fleet.source, "FLEET_NODE", f"logical node {node_id!r} does not exist"))
            elif node.role != expected_role:
                issues.append(
                    ValidationIssue.at(fleet.source, "FLEET_NODE_ROLE", f"{node_id!r} has role {node.role}, expected {expected_role}")
                )

        routing_keys: set[str] = set()
        pairs: set[tuple[str, str]] = set()
        for bridge in fleet.bridges:
            if not bridge.service_credential_ref.startswith(f"secret://kv/{env}/bridges/"):
                issues.append(
                    ValidationIssue.at(
                        fleet.source,
                        "SECRET_ENV",
                        f"bridge credential reference {bridge.routing_key!r} belongs to another environment",
                    )
                )
            if bridge.routing_key in routing_keys:
                issues.append(ValidationIssue.at(fleet.source, "BRIDGE_KEY", f"duplicate routing_key {bridge.routing_key!r}"))
            routing_keys.add(bridge.routing_key)
            pair = (bridge.entry, bridge.exit)
            if pair in pairs:
                issues.append(ValidationIssue.at(fleet.source, "BRIDGE_PAIR", f"duplicate bridge pair {pair!r}"))
            pairs.add(pair)
            if bridge.entry == bridge.exit:
                issues.append(ValidationIssue.at(fleet.source, "BRIDGE_SELF", "bridge entry and exit must differ"))
            if bridge.entry not in fleet.entries:
                issues.append(ValidationIssue.at(fleet.source, "BRIDGE_ENTRY", f"{bridge.entry!r} is not an entry of this fleet"))
            if bridge.exit not in fleet.exits:
                issues.append(ValidationIssue.at(fleet.source, "BRIDGE_EXIT", f"{bridge.exit!r} is not an exit of this fleet"))
            expected_key = f"{bridge.entry}.to-{bridge.exit}"
            if bridge.routing_key != expected_key:
                issues.append(ValidationIssue.at(fleet.source, "BRIDGE_NAME", f"routing_key must be {expected_key!r}"))

    for node in state.nodes:
        count = membership[node.object_id]
        if count > 1:
            issues.append(ValidationIssue.at(node.source, "NODE_MEMBERSHIP", f"logical node may belong to at most one fleet, found {count}"))


def _validate_secrets_and_public_identity(state: DesiredState, issues: list[ValidationIssue]) -> None:
    env = state.environment.object_id
    instances_by_node: dict[str, list[Instance]] = defaultdict(list)
    for instance in state.instances:
        instances_by_node[instance.logical_node].append(instance)
    for node in state.nodes:
        if not node.private_key_ref.startswith(f"secret://kv/{env}/"):
            issues.append(ValidationIssue.at(node.source, "SECRET_ENV", "REALITY private key reference belongs to another environment"))
        for reference in (node.mask_certificate_ref, node.mask_private_key_ref):
            if not reference.startswith(f"secret://kv/{env}/"):
                issues.append(
                    ValidationIssue.at(
                        node.source,
                        "SECRET_ENV",
                        "mask certificate reference belongs to another environment",
                    )
                )
        if node.hostname == node.object_id or node.hostname.startswith(f"{node.object_id}."):
            issues.append(ValidationIssue.at(node.source, "PUBLIC_NAME", "public hostname must not be derived directly from node_id"))
        hostname_in_zone = node.hostname == state.environment.dns_zone or node.hostname.endswith(
            f".{state.environment.dns_zone}"
        )
        if node.role == "entry" and not hostname_in_zone:
            issues.append(
                ValidationIssue.at(
                    node.source,
                    "DNS_ZONE",
                    f"entry public hostname must belong to DNS zone {state.environment.dns_zone!r}",
                )
            )
        for instance in instances_by_node[node.object_id]:
            for field_name, value in (("hostname", node.hostname), ("server_name", node.server_name)):
                if instance.object_id in value:
                    issues.append(
                        ValidationIssue.at(node.source, "INSTANCE_LEAK", f"public {field_name} contains instance_id {instance.object_id!r}")
                    )
