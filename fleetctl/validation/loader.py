"""Schema validation and conversion from YAML into typed objects."""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from fleetctl.model import (
    BotRuntime,
    BridgeRelation,
    CommonConfig,
    ControlPlane,
    Environment,
    Fleet,
    Instance,
    ImmutableImage,
    LogicalNode,
    common_from_values,
)

from .issues import ValidationIssue


API_VERSION = "spiritvpn.io/v1alpha1"
KINDS = ("Environment", "Fleet", "LogicalNode", "Instance")
SCHEMA_FILES = {
    "Environment": "environment.schema.json",
    "Fleet": "fleet.schema.json",
    "LogicalNode": "logical-node.schema.json",
    "Instance": "instance.schema.json",
}
COMMON_SCHEMA_FILES = {
    "components": "components.schema.json",
    "networking": "networking.schema.json",
    "observability": "observability.schema.json",
    "rollout": "rollout.schema.json",
    "xray": "xray.schema.json",
    "limits": "limits.schema.json",
}


def _format_checker() -> FormatChecker:
    checker = FormatChecker()

    @checker.checks("ipv4-network", raises=ValueError)
    def is_ipv4_network(value: object) -> bool:
        if not isinstance(value, str):
            return False
        return isinstance(ipaddress.ip_network(value, strict=True), ipaddress.IPv4Network)

    return checker


def load_schemas(schema_root: Path) -> tuple[dict[str, Draft202012Validator], list[ValidationIssue]]:
    validators: dict[str, Draft202012Validator] = {}
    issues: list[ValidationIssue] = []
    checker = _format_checker()
    for kind, filename in SCHEMA_FILES.items():
        path = schema_root / filename
        try:
            schema = json.loads(path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
            validators[kind] = Draft202012Validator(schema, format_checker=checker)
        except (OSError, ValueError) as exc:
            issues.append(ValidationIssue.at(path, "SCHEMA_LOAD", str(exc)))
    override_path = schema_root / "common-overrides.schema.json"
    try:
        override_schema = json.loads(override_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(override_schema)
        validators["CommonOverrides"] = Draft202012Validator(override_schema, format_checker=checker)
    except (OSError, ValueError) as exc:
        issues.append(ValidationIssue.at(override_path, "SCHEMA_LOAD", str(exc)))
    return validators, issues


def load_common_config(
    common_root: Path,
    schema_root: Path,
) -> tuple[CommonConfig | None, list[ValidationIssue]]:
    issues: list[ValidationIssue] = []
    documents: dict[str, tuple[dict[str, Any], Path]] = {}
    checker = _format_checker()
    for name, schema_filename in COMMON_SCHEMA_FILES.items():
        schema_path = schema_root / schema_filename
        config_path = common_root / f"{name}.yml"
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
            validator = Draft202012Validator(schema, format_checker=checker)
        except (OSError, ValueError) as exc:
            issues.append(ValidationIssue.at(schema_path, "SCHEMA_LOAD", str(exc)))
            continue
        document = _load_yaml_mapping(config_path, issues)
        if document is None:
            continue
        schema_errors = sorted(validator.iter_errors(document), key=lambda error: list(error.absolute_path))
        for error in schema_errors:
            location = ".".join(str(part) for part in error.absolute_path) or "$"
            issues.append(ValidationIssue.at(config_path, "SCHEMA", f"{location}: {error.message}"))
        if not schema_errors:
            documents[name] = (document, config_path)
    if issues or len(documents) != len(COMMON_SCHEMA_FILES):
        return None, issues
    return _to_common_model(documents), issues


def load_environment_objects(
    environment_root: Path,
    validators: dict[str, Draft202012Validator],
) -> tuple[list[object], list[ValidationIssue]]:
    objects: list[object] = []
    issues: list[ValidationIssue] = []
    if not environment_root.is_dir():
        return [], [ValidationIssue.at(environment_root, "ENVIRONMENT_MISSING", "environment directory does not exist")]

    paths = sorted((*environment_root.rglob("*.yml"), *environment_root.rglob("*.yaml")))
    for path in paths:
        document = _load_yaml_mapping(path, issues)
        if document is None:
            continue
        kind = document.get("kind")
        if kind not in KINDS:
            issues.append(ValidationIssue.at(path, "UNKNOWN_KIND", f"unsupported kind {kind!r}"))
            continue
        validator = validators.get(kind)
        if validator is None:
            issues.append(ValidationIssue.at(path, "SCHEMA_UNAVAILABLE", f"schema for {kind} is unavailable"))
            continue
        schema_errors = sorted(validator.iter_errors(document), key=lambda error: list(error.absolute_path))
        for error in schema_errors:
            location = ".".join(str(part) for part in error.absolute_path) or "$"
            issues.append(ValidationIssue.at(path, "SCHEMA", f"{location}: {error.message}"))
        if schema_errors:
            continue

        overrides = document["spec"].get("common_overrides", {})
        override_validator = validators.get("CommonOverrides")
        if override_validator is None:
            issues.append(ValidationIssue.at(path, "SCHEMA_UNAVAILABLE", "common override schema is unavailable"))
            continue
        override_errors = sorted(
            override_validator.iter_errors(overrides),
            key=lambda error: list(error.absolute_path),
        )
        for error in override_errors:
            suffix = ".".join(str(part) for part in error.absolute_path)
            location = "spec.common_overrides" + (f".{suffix}" if suffix else "")
            issues.append(ValidationIssue.at(path, "SCHEMA", f"{location}: {error.message}"))
        if override_errors:
            continue

        object_id = document["metadata"]["id"]
        expected_filename = "environment.yml" if kind == "Environment" else f"{object_id}.yml"
        if path.name != expected_filename:
            issues.append(
                ValidationIssue.at(
                    path,
                    "FILENAME",
                    f"{kind} {object_id!r} must be stored as {expected_filename!r}",
                )
            )
            continue
        objects.append(_to_model(document, path))
    return objects, issues


def load_fleet_ids(path: Path) -> tuple[dict[str, int], list[ValidationIssue]]:
    issues: list[ValidationIssue] = []
    document = _load_yaml_mapping(path, issues)
    if document is None:
        return {}, issues
    result: dict[str, int] = {}
    for key, value in document.items():
        if not isinstance(key, str) or not key:
            issues.append(ValidationIssue.at(path, "FLEET_ID_KEY", "fleet ID registry keys must be non-empty strings"))
        elif not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 2**63 - 1:
            issues.append(
                ValidationIssue.at(
                    path,
                    "FLEET_ID_VALUE",
                    f"{key!r} must map to a positive signed int64",
                )
            )
        else:
            result[key] = value
    return result, issues


def _load_yaml_mapping(path: Path, issues: list[ValidationIssue]) -> dict[str, Any] | None:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        issues.append(ValidationIssue.at(path, "YAML", str(exc)))
        return None
    if not isinstance(document, dict):
        issues.append(ValidationIssue.at(path, "DOCUMENT", "document must be a YAML mapping"))
        return None
    return document


def _to_model(document: dict[str, Any], path: Path) -> object:
    object_id = document["metadata"]["id"]
    spec = document["spec"]
    match document["kind"]:
        case "Environment":
            control_spec = spec.get("control")
            control = None
            if control_spec is not None:
                control = ControlPlane(
                    backend_source_git_sha=control_spec["backend_release"]["source_git_sha"],
                    backend_image=ImmutableImage(**control_spec["backend_release"]["backend_image"]),
                    migration_image=ImmutableImage(**control_spec["backend_release"]["migration_image"]),
                    postgres_image=ImmutableImage(**control_spec["postgres"]["image"]),
                    postgres_exporter_image=ImmutableImage(
                        **control_spec["postgres"]["exporter_image"]
                    ),
                    postgres_major_version=control_spec["postgres"]["major_version"],
                    postgres_database=control_spec["postgres"]["database"],
                    postgres_owner_user=control_spec["postgres"]["owner_user"],
                    postgres_runtime_user=control_spec["postgres"]["runtime_user"],
                    backup_required=control_spec["postgres"]["backup_required"],
                    secret_refs=dict(control_spec["secrets"]),
                    customer_access_writers=tuple(
                        control_spec["authorization"]["customer_access_writers"]
                    ),
                    customer_access_readers=tuple(
                        control_spec["authorization"]["customer_access_readers"]
                    ),
                    bot=_to_bot(control_spec.get("bot")),
                )
            return Environment(
                object_id=object_id,
                dns_zone=spec["dns_zone"],
                management_network=spec["management_network"],
                backend_endpoint=spec["backend_endpoint"],
                secret_kv=spec["secret_store"]["kv"],
                secret_pki=spec["secret_store"]["pki"],
                control=control,
                common_overrides=spec.get("common_overrides", {}),
                source=path,
            )
        case "Fleet":
            return Fleet(
                object_id=object_id,
                entries=tuple(spec["entries"]),
                exits=tuple(spec["exits"]),
                bridges=tuple(
                    BridgeRelation(
                        routing_key=bridge["routing_key"],
                        entry=bridge["entry"],
                        exit=bridge["exit"],
                        display_name=bridge["display_name"],
                        service_credential_ref=bridge["service_credential_ref"],
                    )
                    for bridge in spec["bridges"]
                ),
                source=path,
            )
        case "LogicalNode":
            return LogicalNode(
                object_id=object_id,
                role=spec["role"],
                region=spec["region"],
                display_name=spec["display_name"],
                hostname=spec["public"]["hostname"],
                public_port=spec["public"]["port"],
                transport=spec["public"]["transport"],
                flow=spec["public"]["flow"],
                fingerprint=spec["public"]["fingerprint"],
                server_name=spec["public"]["server_name"],
                reality_public_key=spec["reality"]["public_key"],
                reality_short_id=spec["reality"]["short_id"],
                private_key_ref=spec["reality"]["private_key_ref"],
                mask_certificate_ref=spec["mask"]["certificate_ref"],
                mask_private_key_ref=spec["mask"]["private_key_ref"],
                common_overrides=spec.get("common_overrides", {}),
                source=path,
            )
        case "Instance":
            return Instance(
                object_id=object_id,
                logical_node=spec["logical_node"],
                target_state=spec["target_state"],
                public_address=spec["public_address"],
                # Отсутствие допустимо на этом слое: impact plan валидирует
                # базовый коммит нынешними контрактами, а в нём этого поля ещё
                # не было. Требование живёт в компиляторе known_hosts, который
                # видит, до какого инстанса выкатка вообще дотягивается.
                ssh_host_key=spec.get("ssh_host_key", ""),
                bandwidth_profile=spec["bandwidth_profile"],
                provider_name=spec["provider"]["name"],
                provider_resource_id=spec["provider"]["resource_id"],
                source=path,
            )
    raise AssertionError(f"unreachable kind: {document['kind']}")


def _to_bot(bot_spec: dict[str, Any] | None) -> BotRuntime | None:
    if bot_spec is None:
        return None
    settings = bot_spec["settings"]
    return BotRuntime(
        source_git_sha=bot_spec["release"]["source_git_sha"],
        image=ImmutableImage(**bot_spec["release"]["image"]),
        postgres_database=bot_spec["postgres"]["database"],
        postgres_owner_user=bot_spec["postgres"]["owner_user"],
        postgres_runtime_user=bot_spec["postgres"]["runtime_user"],
        client_identity=settings["client_identity"],
        friends_plan_fleet=settings["friends_plan_fleet"],
        # Absent means "keep the image's own default": the bot already declares
        # one, and repeating it here would create a second place to change it.
        friends_plan_quota_bytes=settings.get("friends_plan_quota_bytes"),
        friends_plan_duration_days=settings.get("friends_plan_duration_days"),
        tunnel_image=ImmutableImage(**bot_spec["ingress"]["image"]),
        public_hostname=bot_spec["ingress"]["hostname"],
        secret_refs=dict(bot_spec["secrets"]),
    )


def _to_common_model(documents: dict[str, tuple[dict[str, Any], Path]]) -> CommonConfig:
    values = {
        name: (
            document["components"]
            if name == "components"
            else {key: value for key, value in document.items() if key != "schema_version"}
        )
        for name, (document, _) in documents.items()
    }
    sources = {name: path for name, (_, path) in documents.items()}
    return common_from_values(values, sources)
