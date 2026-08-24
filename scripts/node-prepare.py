#!/usr/bin/env python3
"""Выпуск материала REALITY для новой ноды и фрагмент топологии под него.

Скрипт **ничего не пишет** — ни в Vault, ни в топологию. Он печатает две вещи:
объект для Vault и объявление для Git. Записывает их церемония, потому что
Vault слушает loopback и принимает только root-токен с терминала оператора, а
топологию правит и коммитит человек: добавление машины в инфраструктуру — это
единственный путь, который обязан проходить ревью.

Разделение половинок между Git и Vault здесь и рождается, поэтому обе выводятся
из одного ключа в одном месте. Расходятся они молча: нода с неверной публичной
половиной поднимается рабочей на вид и отдаёт клиентов маскировочному сайту.
Сверку делает `vault-secret-resolver.py` перед выкаткой; здесь — источник.
"""

from __future__ import annotations

import argparse
import base64
import json
import secrets
import sys
from pathlib import Path
from typing import Any

import yaml

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

# `short_id` схема требует чётной длины hex до 16 символов. Берём максимум:
# он ничего не стоит и оставляет меньше шансов на совпадение между нодами.
SHORT_ID_BYTES = 8


class NodePrepareError(Exception):
    pass


def _encode(raw: bytes) -> str:
    """base64url без padding — форма, в которой ключ понимает xray.

    Та же кодировка используется в топологии, в Vault и при сверке пары, чтобы
    сравнение шло по строкам. Любое расхождение формы означало бы, что сверяется
    не то, что поедет на ноду.
    """
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def generate_reality_keypair() -> tuple[str, str]:
    """Возвращает (приватный, публичный) в форме xray."""
    key = X25519PrivateKey.generate()
    private = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return _encode(private), _encode(public)


def generate_short_id() -> str:
    return secrets.token_hex(SHORT_ID_BYTES)


def logical_node_document(
    *,
    environment: str,
    node_id: str,
    role: str,
    region: str,
    hostname: str,
    server_name: str,
    port: int,
    display_name: str,
    public_key: str,
    short_id: str,
) -> dict[str, Any]:
    secret_root = f"secret://kv/{environment}/nodes/{node_id}"
    return {
        "apiVersion": "spiritvpn.io/v1alpha1",
        "kind": "LogicalNode",
        "metadata": {"id": node_id},
        "spec": {
            "role": role,
            "region": region,
            "public": {
                "hostname": hostname,
                "port": port,
                "transport": "tcp",
                "flow": "xtls-rprx-vision",
                "fingerprint": "chrome",
                "server_name": server_name,
            },
            "reality": {
                "public_key": public_key,
                "short_id": short_id,
                "private_key_ref": f"{secret_root}/reality#private_key",
            },
            "mask": {
                "certificate_ref": f"{secret_root}/mask#fullchain",
                "private_key_ref": f"{secret_root}/mask#private_key",
            },
            "display_name": display_name,
        },
    }


def instance_document(
    *,
    node_id: str,
    slot: int,
    address: str,
    bandwidth_profile: str,
    resource_id: str,
    ssh_host_key: str | None,
) -> dict[str, Any]:
    """Инстанс новой ноды.

    `target_state` — `serving`, и `provisioning` здесь было бы ошибкой, хотя и
    выглядит честнее. Это **желаемое** состояние, а не наблюдаемое: контракт
    требует ровно один обслуживающий инстанс на логическую ноду и отвергает
    объявление с нулём (`SERVING_COUNT`). Нода, которую добавляют, чтобы она
    работала, объявляется работающей; довести её до этого состояния — задача
    выкатки, а не топологии.

    `ssh_host_key` необязателен только здесь: снять его можно лишь с живой
    машины, и до первого контакта его физически нет. Компилятор `known_hosts`
    откажет, если он не появится к выкатке.
    """
    spec: dict[str, Any] = {
        "logical_node": node_id,
        "target_state": "serving",
        "public_address": address,
        "bandwidth_profile": bandwidth_profile,
        "provider": {"name": "manual", "resource_id": resource_id},
    }
    if ssh_host_key:
        spec["ssh_host_key"] = ssh_host_key.strip()
    return {
        "apiVersion": "spiritvpn.io/v1alpha1",
        "kind": "Instance",
        "metadata": {"id": f"{node_id}-{slot:02d}"},
        "spec": spec,
    }


def build(arguments: argparse.Namespace) -> tuple[dict[str, str], list[dict[str, Any]]]:
    if not 1 <= arguments.slot <= 240:
        raise NodePrepareError("слот управления должен быть в диапазоне 1..240")
    private_key, public_key = generate_reality_keypair()
    short_id = generate_short_id()
    node = logical_node_document(
        environment=arguments.environment,
        node_id=arguments.node_id,
        role=arguments.role,
        region=arguments.region,
        hostname=arguments.hostname,
        server_name=arguments.server_name or arguments.hostname,
        port=arguments.port,
        display_name=arguments.display_name,
        public_key=public_key,
        short_id=short_id,
    )
    instance = instance_document(
        node_id=arguments.node_id,
        slot=arguments.slot,
        address=arguments.address,
        bandwidth_profile=arguments.bandwidth_profile,
        resource_id=arguments.resource_id or arguments.node_id,
        ssh_host_key=arguments.ssh_host_key,
    )
    return {"private_key": private_key}, [node, instance]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Выпустить материал REALITY и объявление новой ноды",
    )
    parser.add_argument("--environment", required=True, choices=("develop", "prod"))
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--role", required=True, choices=("entry", "exit"))
    parser.add_argument("--region", required=True)
    parser.add_argument("--hostname", required=True, help="публичное имя ноды")
    parser.add_argument(
        "--server-name",
        help="SNI, если отличается от hostname; по умолчанию совпадает",
    )
    parser.add_argument("--port", type=int, default=443)
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--address", required=True, help="публичный IP машины")
    parser.add_argument("--slot", type=int, required=True, help="слот оверлея, 1..240")
    parser.add_argument("--bandwidth-profile", default="vps-1g")
    parser.add_argument("--resource-id", help="по умолчанию совпадает с --node-id")
    parser.add_argument(
        "--ssh-host-key",
        help="ключ хоста, снятый с живой машины; можно дописать позже",
    )
    parser.add_argument(
        "--secret-output",
        required=True,
        type=Path,
        help="куда положить объект для nodes/<id>/reality; читает церемония",
    )
    arguments = parser.parse_args()
    try:
        secret, documents = build(arguments)
    except NodePrepareError as error:
        print(f"node-prepare: {error}", file=sys.stderr)
        return 2

    # 0600 и запись до печати: если файл не лёг, оператор не должен увидеть
    # фрагмент, публичная половина которого уже никому не парная.
    arguments.secret_output.write_text(json.dumps(secret, indent=2) + "\n", encoding="utf-8")
    arguments.secret_output.chmod(0o600)

    print(yaml.safe_dump_all(documents, sort_keys=False, allow_unicode=True), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
