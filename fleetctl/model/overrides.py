"""Typed common configuration construction and deterministic overrides."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from .objects import (
    BandwidthProfile,
    CommonConfig,
    Component,
    ComponentsConfig,
    LimitsConfig,
    NetworkingConfig,
    ObservabilityConfig,
    RolloutConfig,
    XrayConfig,
)
from .serialization import common_source_values


class CommonOverrideError(ValueError):
    """An override was structurally valid but did not form a complete config."""


def common_from_values(
    values: Mapping[str, Any],
    sources: Mapping[str, Path],
) -> CommonConfig:
    try:
        components = values["components"]
        networking = values["networking"]
        observability = values["observability"]
        rollout = values["rollout"]
        xray = values["xray"]
        limits = values["limits"]
        return CommonConfig(
            components=ComponentsConfig(
                components={
                    name: Component(
                        repository=value["repository"],
                        tag=value["tag"],
                        digest=value["digest"],
                    )
                    for name, value in components.items()
                },
                source=sources["components"],
            ),
            networking=NetworkingConfig(
                management_interface=networking["management"]["interface"],
                management_listen_port=networking["management"]["listen_port"],
                management_mtu=networking["management"]["mtu"],
                persistent_keepalive_seconds=networking["management"]["persistent_keepalive_seconds"],
                ssh_port=networking["ssh"]["port"],
                agent_port=networking["agent"]["port"],
                dns_ttl_seconds=networking["dns"]["ttl_seconds"],
                dns_proxied=networking["dns"]["proxied"],
                source=sources["networking"],
            ),
            observability=ObservabilityConfig(
                operations_retention_days=observability["retention"]["operations_days"],
                activity_retention_days=observability["retention"]["activity_days"],
                node_exporter_port=observability["ports"]["node_exporter"],
                xray_metrics_port=observability["ports"]["xray_metrics"],
                agent_metrics_port=observability["ports"]["agent_metrics"],
                scrape_interval_seconds=observability["scrape_interval_seconds"],
                probe_interval_seconds=observability["probe"]["interval_seconds"],
                probe_timeout_seconds=observability["probe"]["timeout_seconds"],
                source=sources["observability"],
            ),
            rollout=RolloutConfig(
                max_parallel_logical_nodes_per_fleet=rollout["max_parallel_logical_nodes_per_fleet"],
                convergence_timeout_seconds=rollout["convergence_timeout_seconds"],
                drain_timeout_seconds=rollout["drain_timeout_seconds"],
                source=sources["rollout"],
            ),
            xray=XrayConfig(
                inbound_tag=xray["tags"]["inbound"],
                direct_outbound_tag=xray["tags"]["direct_outbound"],
                block_outbound_tag=xray["tags"]["block_outbound"],
                exit_outbound_prefix=xray["tags"]["exit_outbound_prefix"],
                probe_outbound_prefix=xray["tags"]["probe_outbound_prefix"],
                default_outbound_tag=xray["default_outbound_tag"],
                access_log_enabled=xray["access_log"]["enabled"],
                access_log_export_enabled=xray["access_log"]["export_enabled"],
                source=sources["xray"],
            ),
            limits=LimitsConfig(
                bandwidth_profiles={
                    name: BandwidthProfile(
                        port_capacity_mbps=value["port_capacity_mbps"],
                        egress_utilization_percent=value["egress_utilization_percent"],
                        qdisc_kind=value["qdisc"]["kind"],
                        diffserv=value["qdisc"]["diffserv"],
                        entry_flow_isolation=value["qdisc"]["entry_flow_isolation"],
                        exit_flow_isolation=value["qdisc"]["exit_flow_isolation"],
                        nat=value["qdisc"]["nat"],
                        rtt=value["qdisc"]["rtt"],
                    )
                    for name, value in limits["bandwidth_profiles"].items()
                },
                degradation_threshold_percent=limits["degradation_threshold_percent"],
                source=sources["limits"],
            ),
        )
    except (KeyError, TypeError) as exc:
        missing = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
        raise CommonOverrideError(f"override leaves common configuration incomplete at {missing!r}") from exc


def apply_common_overrides(
    base: CommonConfig,
    overrides: Mapping[str, Any],
    source: Path,
) -> CommonConfig:
    if not overrides:
        return base
    values = common_source_values(base)
    _deep_merge(values, overrides)
    sources = {
        "components": base.components.source,
        "networking": base.networking.source,
        "observability": base.observability.source,
        "rollout": base.rollout.source,
        "xray": base.xray.source,
        "limits": base.limits.source,
    }
    for section in overrides:
        sources[section] = source
    return common_from_values(values, sources)


def _deep_merge(target: dict[str, Any], overlay: Mapping[str, Any]) -> None:
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = deepcopy(value)
