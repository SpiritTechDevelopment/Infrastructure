"""Typed, transport-independent desired-state objects."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Component:
    repository: str
    tag: str
    digest: str | None

    @property
    def image(self) -> str:
        suffix = f"@{self.digest}" if self.digest is not None else f":{self.tag}"
        return f"{self.repository}{suffix}"


@dataclass(frozen=True, slots=True)
class ImmutableImage:
    repository: str
    digest: str

    @property
    def image(self) -> str:
        return f"{self.repository}@{self.digest}"


@dataclass(frozen=True, slots=True)
class ControlPlane:
    backend_source_git_sha: str
    backend_image: ImmutableImage
    migration_image: ImmutableImage
    postgres_image: ImmutableImage
    postgres_major_version: int
    postgres_database: str
    postgres_owner_user: str
    postgres_runtime_user: str
    backup_required: bool
    secret_refs: dict[str, str]
    customer_access_writers: tuple[str, ...]
    customer_access_readers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ComponentsConfig:
    components: dict[str, Component]
    source: Path


@dataclass(frozen=True, slots=True)
class NetworkingConfig:
    management_interface: str
    management_listen_port: int
    management_mtu: int
    persistent_keepalive_seconds: int
    ssh_port: int
    agent_port: int
    dns_ttl_seconds: int
    dns_proxied: bool
    source: Path


@dataclass(frozen=True, slots=True)
class ObservabilityConfig:
    operations_retention_days: int
    activity_retention_days: int
    node_exporter_port: int
    xray_metrics_port: int
    agent_metrics_port: int
    scrape_interval_seconds: int
    probe_interval_seconds: int
    probe_timeout_seconds: int
    source: Path


@dataclass(frozen=True, slots=True)
class RolloutConfig:
    max_parallel_logical_nodes_per_fleet: int
    convergence_timeout_seconds: int
    drain_timeout_seconds: int | None
    source: Path


@dataclass(frozen=True, slots=True)
class XrayConfig:
    inbound_tag: str
    direct_outbound_tag: str
    block_outbound_tag: str
    exit_outbound_prefix: str
    probe_outbound_prefix: str
    default_outbound_tag: str
    access_log_enabled: bool
    access_log_export_enabled: bool
    source: Path


@dataclass(frozen=True, slots=True)
class BandwidthProfile:
    port_capacity_mbps: int
    egress_utilization_percent: int
    qdisc_kind: str
    diffserv: str
    entry_flow_isolation: str
    exit_flow_isolation: str
    nat: bool
    rtt: str

    @property
    def egress_limit_mbps(self) -> int:
        return self.port_capacity_mbps * self.egress_utilization_percent // 100

    def flow_isolation_for(self, role: str) -> str:
        return self.entry_flow_isolation if role == "entry" else self.exit_flow_isolation


@dataclass(frozen=True, slots=True)
class LimitsConfig:
    bandwidth_profiles: dict[str, BandwidthProfile]
    degradation_threshold_percent: int | None
    source: Path

@dataclass(frozen=True, slots=True)
class CommonConfig:
    components: ComponentsConfig
    networking: NetworkingConfig
    observability: ObservabilityConfig
    rollout: RolloutConfig
    xray: XrayConfig
    limits: LimitsConfig


@dataclass(frozen=True, slots=True)
class Environment:
    object_id: str
    dns_zone: str
    management_network: str
    backend_endpoint: str
    secret_kv: str
    secret_pki: str
    control: ControlPlane | None
    common_overrides: dict[str, Any]
    source: Path


@dataclass(frozen=True, slots=True)
class BridgeRelation:
    routing_key: str
    entry: str
    exit: str
    display_name: str
    service_credential_ref: str


@dataclass(frozen=True, slots=True)
class Fleet:
    object_id: str
    entries: tuple[str, ...]
    exits: tuple[str, ...]
    bridges: tuple[BridgeRelation, ...]
    source: Path


@dataclass(frozen=True, slots=True)
class LogicalNode:
    object_id: str
    role: str
    region: str
    display_name: str
    hostname: str
    public_port: int
    transport: str
    flow: str
    fingerprint: str
    server_name: str
    reality_public_key: str
    reality_short_id: str
    private_key_ref: str
    mask_certificate_ref: str
    mask_private_key_ref: str
    common_overrides: dict[str, Any]
    source: Path


@dataclass(frozen=True, slots=True)
class Instance:
    object_id: str
    logical_node: str
    target_state: str
    public_address: str
    bandwidth_profile: str
    provider_name: str
    provider_resource_id: str
    source: Path


@dataclass(frozen=True, slots=True)
class DesiredState:
    common: CommonConfig
    environment_common: CommonConfig
    node_common: dict[str, CommonConfig]
    environment: Environment
    fleets: tuple[Fleet, ...]
    nodes: tuple[LogicalNode, ...]
    instances: tuple[Instance, ...]
    fleet_ids: dict[str, int]

    def common_for_node(self, node_id: str) -> CommonConfig:
        return self.node_common[node_id]
