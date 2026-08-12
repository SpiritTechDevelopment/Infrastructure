"""Stable serialization of transport-independent model values."""

from __future__ import annotations

from typing import Any

from .objects import CommonConfig


def common_values(common: CommonConfig) -> dict[str, Any]:
    values = common_source_values(common)
    for name, profile in common.limits.bandwidth_profiles.items():
        values["limits"]["bandwidth_profiles"][name]["egress_limit_mbps"] = profile.egress_limit_mbps
    return values


def common_source_values(common: CommonConfig) -> dict[str, Any]:
    """Serialize only declarative fields, excluding derived values."""

    return {
        "components": {
            name: {
                "repository": component.repository,
                "tag": component.tag,
                "digest": component.digest,
            }
            for name, component in sorted(common.components.components.items())
        },
        "networking": {
            "management": {
                "interface": common.networking.management_interface,
                "listen_port": common.networking.management_listen_port,
                "mtu": common.networking.management_mtu,
                "persistent_keepalive_seconds": common.networking.persistent_keepalive_seconds,
            },
            "agent": {"port": common.networking.agent_port},
            "dns": {
                "ttl_seconds": common.networking.dns_ttl_seconds,
                "proxied": common.networking.dns_proxied,
            },
        },
        "observability": {
            "retention": {
                "operations_days": common.observability.operations_retention_days,
                "activity_days": common.observability.activity_retention_days,
            },
            "ports": {
                "node_exporter": common.observability.node_exporter_port,
                "xray_metrics": common.observability.xray_metrics_port,
            },
            "scrape_interval_seconds": common.observability.scrape_interval_seconds,
            "probe": {
                "interval_seconds": common.observability.probe_interval_seconds,
                "timeout_seconds": common.observability.probe_timeout_seconds,
            },
        },
        "rollout": {
            "max_parallel_logical_nodes_per_fleet": common.rollout.max_parallel_logical_nodes_per_fleet,
            "convergence_timeout_seconds": common.rollout.convergence_timeout_seconds,
            "drain_timeout_seconds": common.rollout.drain_timeout_seconds,
        },
        "xray": {
            "tags": {
                "inbound": common.xray.inbound_tag,
                "direct_outbound": common.xray.direct_outbound_tag,
                "block_outbound": common.xray.block_outbound_tag,
                "exit_outbound_prefix": common.xray.exit_outbound_prefix,
                "probe_outbound_prefix": common.xray.probe_outbound_prefix,
            },
            "default_outbound_tag": common.xray.default_outbound_tag,
            "access_log": {
                "enabled": common.xray.access_log_enabled,
                "export_enabled": common.xray.access_log_export_enabled,
            },
        },
        "limits": {
            "bandwidth_profiles": {
                name: {
                    "port_capacity_mbps": profile.port_capacity_mbps,
                    "egress_utilization_percent": profile.egress_utilization_percent,
                    "qdisc": {
                        "kind": profile.qdisc_kind,
                        "diffserv": profile.diffserv,
                        "entry_flow_isolation": profile.entry_flow_isolation,
                        "exit_flow_isolation": profile.exit_flow_isolation,
                        "nat": profile.nat,
                        "rtt": profile.rtt,
                    },
                }
                for name, profile in sorted(common.limits.bandwidth_profiles.items())
            },
            "degradation_threshold_percent": common.limits.degradation_threshold_percent,
        },
    }
