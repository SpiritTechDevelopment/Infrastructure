#!/usr/bin/env python3
"""Выпуск материала новой ноды: приватное — в Vault, объявление — на печать.

Запускается на хабе, потому что Vault слушает loopback: приватная половина пары
REALITY рождается там же, где хранится, и машины оператора не касается. В Vault
скрипт ходит ролью `node-issuer-<environment>` — она умеет писать только под
`nodes/` и только `create`. Корневой токен не нужен, и это не удобство: с
чтением токена из терминала команду не мог бы вызвать ни раннер, ни любая
автоматика.

В топологию скрипт **не пишет** — печатает объявление. Коммитит и отправляет на
ревью человек: добавление машины в инфраструктуру обязано проходить через диффф.

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
import ssl
import sys
import urllib.error
import urllib.request
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


class VaultHTTPError(NodePrepareError):
    """Отказ Vault с кодом. Код нужен: 404 — это «нет пути», а не сбой."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


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
    transport: str,
    xhttp_path: str | None,
    xhttp_mode: str,
) -> dict[str, Any]:
    secret_root = f"secret://kv/{environment}/nodes/{node_id}"
    public: dict[str, Any] = {
        "hostname": hostname,
        "port": port,
        "transport": transport,
        "flow": "" if transport == "xhttp" else "xtls-rprx-vision",
        "fingerprint": "chrome",
        "server_name": server_name,
    }
    if transport == "xhttp":
        public["xhttp"] = {"path": xhttp_path, "mode": xhttp_mode}

    return {
        "apiVersion": "spiritvpn.io/v1alpha1",
        "kind": "LogicalNode",
        "metadata": {"id": node_id},
        "spec": {
            "role": role,
            "region": region,
            "public": public,
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
    bootstrap_port: int,
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
    # 22 не объявляется: значение по умолчанию, записанное явно, превращает
    # «здесь особый порт» в шум, который перестают замечать.
    if bootstrap_port != 22:
        spec["bootstrap_port"] = bootstrap_port
    return {
        "apiVersion": "spiritvpn.io/v1alpha1",
        "kind": "Instance",
        "metadata": {"id": f"{node_id}-{slot:02d}"},
        "spec": spec,
    }


class VaultClient:
    """AppRole-клиент под политику `node-issuer-<environment>`.

    Отдельный от резолверного намеренно: тот читает секреты всего окружения и
    ничего не пишет, этот пишет только под `nodes/`. Общий клиент означал бы
    общий набор прав, а разделение прав здесь и есть смысл второй роли.

    Логин идёт по HTTP из процесса, а не аргументами `vault` внутри контейнера:
    secret-id в argv виден любому, кто прочитает список процессов.
    """

    def __init__(self, address: str, ca_file: Path, role_id: str, secret_id: str):
        self.address = address.rstrip("/")
        self.context = ssl.create_default_context(cafile=str(ca_file))
        self.token: str | None = None
        response = self._request(
            "POST",
            "/v1/auth/approle/login",
            {"role_id": role_id, "secret_id": secret_id},
        )
        try:
            self.token = response["auth"]["client_token"]
        except (KeyError, TypeError) as exc:
            raise NodePrepareError("Vault AppRole login returned no client token") from exc

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.token is not None:
            headers["X-Vault-Token"] = self.token
        request = urllib.request.Request(
            f"{self.address}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=10) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise VaultHTTPError(exc.code, f"Vault request failed for {path}") from exc
        except (OSError, urllib.error.URLError) as exc:
            raise NodePrepareError(f"Vault request failed for {path}") from exc
        if not body:
            return {}
        try:
            value = json.loads(body)
        except json.JSONDecodeError as exc:
            raise NodePrepareError(f"Vault returned an invalid response for {path}") from exc
        if not isinstance(value, dict):
            raise NodePrepareError(f"Vault returned an invalid response for {path}")
        return value

    def exists(self, path: str) -> bool:
        try:
            self._request("GET", f"/v1/kv/data/{path}")
        except VaultHTTPError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    def read_object(self, path: str) -> dict[str, Any]:
        response = self._request("GET", f"/v1/kv/data/{path}")
        try:
            data = response["data"]["data"]
        except (KeyError, TypeError) as exc:
            raise NodePrepareError(f"Vault path holds no data: kv/{path}") from exc
        if not isinstance(data, dict) or not data:
            raise NodePrepareError(f"Vault path is empty: kv/{path}")
        return data

    def create_object(self, path: str, data: dict[str, str]) -> None:
        """Создаёт путь, отказываясь перезаписать существующий.

        `cas: 0` — «пиши, только если версии ещё нет». Проверка чтением перед
        записью оставляет окно между ними; здесь отказ принимает сам Vault, и
        одновременный второй выпуск не сможет затереть первый. Политика тоже не
        даёт `update`, так что это второй рубеж, а не единственный.
        """
        try:
            self._request(
                "POST",
                f"/v1/kv/data/{path}",
                {"data": data, "options": {"cas": 0}},
            )
        except VaultHTTPError as exc:
            if exc.status == 400:
                raise NodePrepareError(
                    f"kv/{path} уже существует; перевыпуск материала живой ноды "
                    "убил бы её вместе со всеми входами, которые на неё смотрят"
                ) from exc
            raise

    def revoke_self(self) -> None:
        if self.token is None:
            return
        self.token, token = None, self.token
        request = urllib.request.Request(
            f"{self.address}/v1/auth/token/revoke-self",
            data=b"",
            headers={"X-Vault-Token": token},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=10):
                pass
        except (OSError, urllib.error.URLError) as exc:
            raise NodePrepareError("Vault self-revocation failed") from exc


def store(
    client: VaultClient,
    *,
    environment: str,
    node_id: str,
    source_node: str,
    reality: dict[str, str],
) -> None:
    """Кладёт материал новой ноды: ключ REALITY и копию маски.

    Порядок проверок важнее порядка записей. Всё, что может отказать, отказывает
    до первой записи — иначе в Vault остаётся половина результата: ключ без
    сертификата, который ничем не отличить от целого набора.
    """
    reality_path = f"{environment}/nodes/{node_id}/reality"
    mask_path = f"{environment}/nodes/{node_id}/mask"
    if client.exists(reality_path):
        raise NodePrepareError(
            f"kv/{reality_path} уже существует; выпуск для этой ноды уже был"
        )
    if client.exists(mask_path):
        raise NodePrepareError(f"kv/{mask_path} уже существует; выпуск для этой ноды уже был")

    # Сертификат маски — wildcard на весь флот, поэтому новой ноде нужна копия,
    # а не выпуск. Читается до записей: ненайденный источник обязан остановить
    # операцию раньше, чем в Vault появится хоть что-то.
    source = client.read_object(f"{environment}/nodes/{source_node}/mask")
    missing = [name for name in ("fullchain", "private_key") if not source.get(name)]
    if missing:
        raise NodePrepareError(
            f"kv/{environment}/nodes/{source_node}/mask не содержит: {', '.join(missing)}"
        )

    client.create_object(reality_path, reality)
    client.create_object(mask_path, {name: source[name] for name in ("fullchain", "private_key")})


def build(arguments: argparse.Namespace) -> tuple[dict[str, str], list[dict[str, Any]]]:
    if not 1 <= arguments.slot <= 240:
        raise NodePrepareError("слот управления должен быть в диапазоне 1..240")
    if arguments.transport == "xhttp" and not arguments.xhttp_path:
        raise NodePrepareError("для --transport xhttp обязателен --xhttp-path")
    if arguments.transport != "xhttp" and arguments.xhttp_path:
        raise NodePrepareError("--xhttp-path имеет смысл только при --transport xhttp")
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
        transport=arguments.transport,
        xhttp_path=arguments.xhttp_path,
        xhttp_mode=arguments.xhttp_mode,
    )
    instance = instance_document(
        node_id=arguments.node_id,
        slot=arguments.slot,
        address=arguments.address,
        bandwidth_profile=arguments.bandwidth_profile,
        resource_id=arguments.resource_id or arguments.node_id,
        ssh_host_key=arguments.ssh_host_key,
        bootstrap_port=arguments.bootstrap_port,
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
    parser.add_argument("--transport", default="tcp", choices=("tcp", "xhttp"))
    parser.add_argument(
        "--xhttp-path",
        help="путь XHTTP, обязателен при --transport xhttp",
    )
    parser.add_argument(
        "--xhttp-mode",
        default="packet-up",
        choices=("auto", "packet-up", "stream-up", "stream-one"),
        help="режим XHTTP в клиентской ссылке",
    )
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--address", required=True, help="публичный IP машины")
    parser.add_argument("--slot", type=int, required=True, help="слот оверлея, 1..240")
    parser.add_argument("--bandwidth-profile", default="vps-1g")
    parser.add_argument("--resource-id", help="по умолчанию совпадает с --node-id")
    parser.add_argument(
        "--bootstrap-port",
        type=int,
        default=22,
        help="порт sshd до бутстрапа; объявляется только если не 22",
    )
    parser.add_argument(
        "--ssh-host-key",
        help="ключ хоста, снятый с живой машины; можно дописать позже",
    )
    parser.add_argument(
        "--mask-source-node",
        required=True,
        help="нода, чей wildcard-сертификат покрывает имя новой",
    )
    parser.add_argument(
        "--credentials-dir",
        required=True,
        type=Path,
        help="каталог с role-id и secret-id роли node-issuer",
    )
    parser.add_argument("--vault-address", default="https://127.0.0.1:8200")
    parser.add_argument(
        "--vault-ca",
        type=Path,
        default=Path("/opt/spiritvpn/platform/vault/tls/vault-ca.crt"),
    )
    arguments = parser.parse_args()

    client = None
    try:
        secret, documents = build(arguments)
        role_id = (arguments.credentials_dir / "role-id").read_text(encoding="utf-8").strip()
        secret_id = (arguments.credentials_dir / "secret-id").read_text(encoding="utf-8").strip()
        if not role_id or not secret_id:
            raise NodePrepareError("Vault AppRole credentials are empty")
        client = VaultClient(arguments.vault_address, arguments.vault_ca, role_id, secret_id)
        store(
            client,
            environment=arguments.environment,
            node_id=arguments.node_id,
            source_node=arguments.mask_source_node,
            reality=secret,
        )
    except NodePrepareError as error:
        print(f"node-prepare: {error}", file=sys.stderr)
        return 2
    finally:
        # Отзыв и на пути отказа: невостребованный токен иначе живёт весь TTL
        # как готовый к употреблению доступ на запись под `nodes/`.
        if client is not None:
            try:
                client.revoke_self()
            except NodePrepareError as error:
                print(f"warning: {error}", file=sys.stderr)

    # Печать после записи: объявление с публичной половиной имеет смысл только
    # тогда, когда приватная уже в Vault. Обратный порядок дал бы оператору
    # готовый к коммиту фрагмент, парного ключа к которому нет нигде.
    print(yaml.safe_dump_all(documents, sort_keys=False, allow_unicode=True), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
