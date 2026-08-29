#!/usr/bin/env python3
"""Resolve environment-scoped secret:// references on the management executor."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import ssl
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from fleetctl.validation import DesiredStateInvalid, validate_environment


REFERENCE = re.compile(
    r"^secret://kv/(develop|prod)/([A-Za-z0-9._/-]+)#([A-Za-z_][A-Za-z0-9_]*)$"
)

# Один путь Vault — один файл на диске. Инфраструктура знает, какой путь
# наполняет какой файл, и не знает, что внутри: состав окружения принадлежит
# компоненту, а не топологии. Поэтому здесь нет ни перечня переменных, ни
# схемы для них — только адреса.
#
# `files` держит материал, который раздаётся файлами, а не переменными:
# имя поля становится именем файла. Он вынесен отдельным путём намеренно —
# признак «в файл или в окружение» иначе пришлось бы кодировать соглашением
# об именах полей, то есть магией внутри значения, которое пишет оператор.
CONTROL_OBJECTS: dict[str, dict[str, str]] = {
    "backend": {
        "env": "control/backend/env",
        "migration_env": "control/backend/migration-env",
        "files": "control/backend/files",
        "postgres": "control/backend/postgres",
    },
    "bot": {
        "env": "control/bot/env",
        "migration_env": "control/bot/migration-env",
        "tunnel_env": "control/bot/tunnel-env",
        "files": "control/bot/files",
        "postgres": "control/bot/postgres",
    },
}

# Единственный объект с объявленным составом, и на то есть причина. Остальные
# инфраструктура переносит, не читая; эти два пароля она выполняет сама —
# `ALTER ROLE <роль> PASSWORD` при провизионинге, — поэтому обязана знать, какой
# из них owner, а какой runtime. Пароль внутри DATABASE_URL для этого не годится.
POSTGRES_FIELDS = ("owner_password", "runtime_password")

# Compose принимает shell-подобные имена переменных и сохраняет их регистр.
# Строчные имена могут быть частью контракта компонента, поэтому инфраструктура
# проверяет только безопасную для env-файла форму, а не навязывает верхний регистр.
ENVIRONMENT_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ResolverError(Exception):
    pass


def clean_environment_object(path: str, data: dict[str, Any]) -> dict[str, str]:
    """Проверяет форму объекта, который целиком станет env-файлом.

    Перевод строки внутри значения отвергается, а хвостовой — срезается. Это
    не вкусовщина, а формат: значения пишутся по одному на строку, поэтому
    разрыв внутри значения молча съедает следующую переменную. Хвостовой при
    этом появляется от того, как значение вводили — церемония записи читает до
    EOF, и любая вставка, законченная Enter, несёт его с собой.
    """
    cleaned: dict[str, str] = {}
    for key, value in sorted(data.items()):
        if not ENVIRONMENT_KEY.fullmatch(key):
            raise ResolverError(f"kv/{path}: invalid environment key {key!r}")
        if not isinstance(value, str):
            raise ResolverError(f"kv/{path}#{key} must be a string")
        stripped = value.strip()
        if not stripped:
            raise ResolverError(f"kv/{path}#{key} is empty")
        if "\n" in stripped or "\r" in stripped:
            raise ResolverError(f"kv/{path}#{key} contains a line break")
        cleaned[key] = stripped
    return cleaned


def clean_postgres_object(path: str, data: dict[str, Any]) -> dict[str, str]:
    """Проверяет два пароля, которые исполнитель применяет сам.

    Состав здесь закрыт: лишнее поле — это либо опечатка, либо секрет, который
    оператор положил не туда и считает применённым. Перевод строки отвергается
    так же, как в окружении: пароль должен совпадать с тем, что внутри DSN,
    байт в байт, иначе роль создаётся с одним значением, а подключение идёт с
    другим — отказ аутентификации с двумя одинаковыми на вид строками.
    """
    missing = [field for field in POSTGRES_FIELDS if field not in data]
    if missing:
        raise ResolverError(f"kv/{path}: missing field(s): {', '.join(missing)}")
    unexpected = sorted(set(data) - set(POSTGRES_FIELDS))
    if unexpected:
        raise ResolverError(f"kv/{path}: unexpected field(s): {', '.join(unexpected)}")
    cleaned: dict[str, str] = {}
    for field in POSTGRES_FIELDS:
        value = data[field]
        if not isinstance(value, str):
            raise ResolverError(f"kv/{path}#{field} must be a string")
        stripped = value.strip()
        if not stripped:
            raise ResolverError(f"kv/{path}#{field} is empty")
        if "\n" in stripped or "\r" in stripped:
            raise ResolverError(f"kv/{path}#{field} contains a line break")
        cleaned[field] = stripped
    return cleaned


def clean_file_object(path: str, data: dict[str, Any]) -> dict[str, str]:
    """Проверяет объект, каждое поле которого станет отдельным файлом.

    Здесь перевод строки законен — это PEM. Проверяется имя: оно становится
    именем файла в защищённом каталоге, поэтому косая черта и `..` отвергаются.
    """
    cleaned: dict[str, str] = {}
    for key, value in sorted(data.items()):
        if not FILE_NAME.fullmatch(key) or ".." in key:
            raise ResolverError(f"kv/{path}: invalid file name {key!r}")
        if not isinstance(value, str) or not value.strip():
            raise ResolverError(f"kv/{path}#{key} must be a non-empty string")
        cleaned[key] = value
    return cleaned


def parse_reference(reference: str, environment: str) -> tuple[str, str]:
    match = REFERENCE.fullmatch(reference)
    if match is None:
        raise ResolverError(f"invalid secret reference: {reference}")
    reference_environment, path, field = match.groups()
    if reference_environment != environment:
        raise ResolverError("cross-environment secret reference refused")
    if "//" in f"/{path}/" or ".." in path.split("/"):
        raise ResolverError("unsafe secret path refused")
    return f"{environment}/{path}", field


def desired_references(
    root: Path,
    environment: str,
    desired_root: Path | None = None,
    scope: str = "all",
) -> list[str]:
    state = validate_environment(root, environment, desired_root=desired_root)
    references: set[str] = set()
    if scope in {"all", "fleet"}:
        references.update(
            reference
            for node in state.nodes
            for reference in (
                node.private_key_ref,
                node.mask_certificate_ref,
                node.mask_private_key_ref,
            )
        )
        references.update(
            bridge.service_credential_ref
            for fleet in state.fleets
            for bridge in fleet.bridges
        )
    # Контур control ссылок в топологии больше не объявляет: его секреты
    # читаются объектами целиком по адресам из CONTROL_OBJECTS.
    return sorted(references)


def reality_public_keys(
    root: Path,
    environment: str,
    desired_root: Path | None = None,
) -> dict[str, str]:
    """Ссылка на приватный ключ REALITY → публичный ключ, объявленный в Git.

    Половинки пары живут в разных хранилищах намеренно, и до этого места они
    ни разу не встречаются: схема принуждает объявить ссылку, Vault хранит
    значение, и никто не сверяет, что одно выведено из другого.
    """
    state = validate_environment(root, environment, desired_root=desired_root)
    return {node.private_key_ref: node.reality_public_key for node in state.nodes}


def verify_reality_pairs(resolved: dict[str, str], declared: dict[str, str]) -> None:
    """Отвергает пару, в которой публичный ключ не выводится из приватного.

    Разошедшаяся пара не ломает ничего заметного: нода поднимается, метрики
    зелёные, а REALITY молча отдаёт клиентов маскировочному сайту. Тот же
    публичный ключ служит паролем на входе, поэтому расхождение рвёт и мосты
    к этому выходу. Отказ здесь — единственное место, где это видно до того,
    как выкатка тронет ноду.
    """
    for reference, public_key in sorted(declared.items()):
        private_key = resolved.get(reference)
        if private_key is None:
            continue
        try:
            derived = derive_reality_public_key(private_key)
        except ValueError as exc:
            raise ResolverError(f"{reference}: {exc}") from exc
        if derived != public_key.strip():
            raise ResolverError(
                f"{reference}: объявленный reality.public_key не соответствует "
                "приватному ключу в Vault; нода поднялась бы рабочей на вид и "
                "отдавала бы клиентов маскировочному сайту"
            )


def derive_reality_public_key(private_key: str) -> str:
    """base64url приватного ключа X25519 → base64url публичного, как у xray.

    Без padding и с urlsafe-алфавитом — в этой форме ключ лежит в Vault, в
    топологии и в конфиге xray, поэтому сравнение идёт по строкам, а не по
    байтам: любая другая форма означала бы, что сверяется не то, что поедет.
    """
    raw = private_key.strip()
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ValueError("приватный ключ REALITY не является base64url") from exc
    if len(decoded) != 32:
        raise ValueError(
            f"приватный ключ REALITY должен быть 32 байта, получено {len(decoded)}"
        )
    public = X25519PrivateKey.from_private_bytes(decoded).public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw
    )
    return base64.urlsafe_b64encode(public).decode("ascii").rstrip("=")


def control_object_paths(root: Path, environment: str, desired_root: Path | None) -> dict[str, dict[str, str]]:
    """Адреса объектов Vault, которые наполняют файлы контура control.

    Бот объявлен не в каждом окружении, поэтому его пути возвращаются только
    когда он есть: чтение отсутствующего пути иначе валило бы выкатку там, где
    бота и не должно быть.
    """
    state = validate_environment(root, environment, desired_root=desired_root)
    control = state.environment.control
    if control is None:
        return {}
    components = ["backend"] if control.bot is None else ["backend", "bot"]
    return {
        component: {
            kind: f"{environment}/{suffix}"
            for kind, suffix in CONTROL_OBJECTS[component].items()
        }
        for component in components
    }


class VaultClient:
    def __init__(self, address: str, ca_file: Path, role_id: str, secret_id: str):
        self.address = address.rstrip("/")
        self.context = ssl.create_default_context(cafile=str(ca_file))
        response = self._request(
            "POST",
            "/v1/auth/approle/login",
            {"role_id": role_id, "secret_id": secret_id},
        )
        try:
            self.token = response["auth"]["client_token"]
        except (KeyError, TypeError) as exc:
            raise ResolverError("Vault AppRole login returned no client token") from exc

    def _request(self, method: str, path: str, payload: dict[str, str] | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        token = getattr(self, "token", None)
        if token is not None:
            headers["X-Vault-Token"] = token
        request = urllib.request.Request(
            f"{self.address}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=10) as response:
                value = json.load(response)
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise ResolverError(f"Vault request failed for {path}") from exc
        if not isinstance(value, dict):
            raise ResolverError(f"Vault returned an invalid response for {path}")
        return value

    def revoke_self(self) -> None:
        """Give the AppRole token back as soon as the secrets are on disk.

        Without this the token stays valid for its whole TTL after the process
        exits, which is a usable Vault credential lying around on the executor
        for no reason. Revocation returns 204 with an empty body, so this does
        not go through `_request`, which expects JSON.
        """
        token = getattr(self, "token", None)
        if token is None:
            return
        self.token = None
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
            raise ResolverError("Vault self-revocation failed") from exc

    def read(self, path: str, field: str) -> str:
        response = self._request("GET", f"/v1/kv/data/{path}")
        try:
            value = response["data"]["data"][field]
        except (KeyError, TypeError) as exc:
            raise ResolverError(f"Vault field is missing: kv/{path}#{field}") from exc
        if not isinstance(value, str) or not value:
            raise ResolverError(f"Vault field must be a non-empty string: kv/{path}#{field}")
        return value

    def read_object(self, path: str) -> dict[str, Any]:
        """Читает путь Vault целиком.

        Пустой или отсутствующий путь — отказ, а не пустой файл. Молча
        отрендерить env-файл без переменных значит выкатить контейнер, который
        упадёт на старте, и разбираться потом с симптомом вместо причины.
        """
        response = self._request("GET", f"/v1/kv/data/{path}")
        try:
            data = response["data"]["data"]
        except (KeyError, TypeError) as exc:
            raise ResolverError(f"Vault path holds no data: kv/{path}") from exc
        if not isinstance(data, dict) or not data:
            raise ResolverError(f"Vault path is empty: kv/{path}")
        return data


def write_private(path: Path, value: str) -> None:
    if path.is_symlink():
        raise ResolverError(f"refusing symlink output: {path}")
    path = path.absolute()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--desired-root", type=Path, help="explicit desired/ fixture (tests only)")
    parser.add_argument("--environment", required=True, choices=("develop", "prod"))
    parser.add_argument("--credentials-dir", type=Path)
    parser.add_argument("--compiled-secrets", type=Path)
    parser.add_argument("--ssh-private-key", type=Path)
    parser.add_argument("--cloudflare-token-file", type=Path)
    parser.add_argument("--scope", choices=("all", "fleet", "control"), default="all")
    parser.add_argument("--list-references", action="store_true")
    parser.add_argument("--list-objects", action="store_true")
    parser.add_argument("--vault-address", default="https://127.0.0.1:8200")
    parser.add_argument(
        "--vault-ca",
        type=Path,
        default=Path("/opt/spiritvpn/platform/vault/tls/vault-ca.crt"),
    )
    args = parser.parse_args()
    try:
        references = desired_references(
            args.root,
            args.environment,
            args.desired_root,
            args.scope,
        )
        ssh_reference = f"secret://kv/{args.environment}/executor/ansible#private_key"
        cloudflare_reference = (
            f"secret://kv/{args.environment}/dns/cloudflare#api_token"
        )
        objects: dict[str, dict[str, str]] = {}
        if args.scope in {"all", "control"}:
            objects = control_object_paths(args.root, args.environment, args.desired_root)
        if args.list_references:
            listed_references = references
            if args.scope in {"all", "fleet"}:
                listed_references = sorted(
                    (*references, ssh_reference, cloudflare_reference)
                )
            for reference in listed_references:
                print(reference)
            return 0
        if args.list_objects:
            # Отдельно от ссылок намеренно: это разные сущности. Ссылка
            # адресует поле, путь — объект целиком, и смешивать их в одном
            # списке значит заставить читателя различать их по форме строки.
            for component in sorted(objects):
                for kind in sorted(objects[component]):
                    print(f"kv/{objects[component][kind]}")
            return 0
        if args.cloudflare_token_file is not None and args.scope not in {"all", "fleet"}:
            raise ResolverError("Cloudflare token output requires fleet or all scope")
        if args.credentials_dir is None or args.compiled_secrets is None:
            raise ResolverError(
                "resolution requires --credentials-dir and --compiled-secrets"
            )
        role_id = (args.credentials_dir / "role-id").read_text(encoding="utf-8").strip()
        secret_id = (args.credentials_dir / "secret-id").read_text(encoding="utf-8").strip()
        if not role_id or not secret_id:
            raise ResolverError("Vault AppRole credentials are empty")
        client = VaultClient(args.vault_address, args.vault_ca, role_id, secret_id)
        try:
            resolved = {}
            for reference in references:
                path, field = parse_reference(reference, args.environment)
                resolved[reference] = client.read(path, field)
            if args.scope in {"all", "fleet"}:
                verify_reality_pairs(
                    resolved,
                    reality_public_keys(args.root, args.environment, args.desired_root),
                )
            control_secrets: dict[str, dict[str, dict[str, str]]] = {}
            for component in sorted(objects):
                collected: dict[str, dict[str, str]] = {}
                for kind, path in sorted(objects[component].items()):
                    data = client.read_object(path)
                    cleaner = {
                        "files": clean_file_object,
                        "postgres": clean_postgres_object,
                    }.get(kind, clean_environment_object)
                    collected[kind] = cleaner(path, data)
                control_secrets[component] = collected
            document: dict[str, Any] = {"spiritvpn_secret_values": resolved}
            if control_secrets:
                document["spiritvpn_control_secrets"] = control_secrets
            payload = yaml.safe_dump(
                document,
                allow_unicode=True,
                sort_keys=True,
            )
            write_private(args.compiled_secrets, payload)
            if args.ssh_private_key is not None:
                ssh_path, ssh_field = parse_reference(ssh_reference, args.environment)
                ssh_private_key = client.read(ssh_path, ssh_field)
                if "PRIVATE KEY" not in ssh_private_key:
                    raise ResolverError("executor Ansible private key has an invalid format")
                write_private(args.ssh_private_key, ssh_private_key.rstrip("\n") + "\n")
            if args.cloudflare_token_file is not None:
                cloudflare_path, cloudflare_field = parse_reference(
                    cloudflare_reference,
                    args.environment,
                )
                cloudflare_token = client.read(cloudflare_path, cloudflare_field).strip()
                if not cloudflare_token or "\n" in cloudflare_token or "\r" in cloudflare_token:
                    raise ResolverError(
                        f"{cloudflare_reference} must be one non-empty line"
                    )
                write_private(args.cloudflare_token_file, cloudflare_token + "\n")
        finally:
            # Revocation runs on the failure path too, where a leftover token
            # matters most. It never changes the outcome: the secrets are
            # already written, and failing the deployment over cleanup would
            # leave the caller worse off than the warning does.
            try:
                client.revoke_self()
            except ResolverError as exc:
                print(f"warning: {exc}", file=sys.stderr)
    except (OSError, ResolverError, DesiredStateInvalid, ValueError) as exc:
        print(f"secret resolution failed: {exc}", file=sys.stderr)
        return 2
    # Печатается количество, не состав: транскрипт исполнителя читают и хранят,
    # а имена переменных бота — уже подсказка о его устройстве.
    objects_read = sum(len(kinds) for kinds in objects.values())
    print(
        f"{args.environment}: resolved {len(resolved)} desired-state secret reference(s) "
        f"and {objects_read} control secret object(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
