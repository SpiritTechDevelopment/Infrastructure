#!/usr/bin/env python3
"""Создаёт операторскую личность на машине её будущего владельца.

Приватные ключи генерируются здесь и отсюда не уходят: в Git, в бандл и в чужие
руки попадают только публичные части. Обратное направление — когда выдающий
оператор генерирует ключи за нового и передаёт их — ломает сразу три вещи:
шифровать такой набор не для кого (у получателя ещё нет ключа, значит остаётся
общая парольная фраза), приватный ключ навсегда остаётся в истории Git, и
неотказуемость исчезает, потому что ключами владеют двое.

Вывод — запрос на включение. Он публичный: его можно передать любым каналом,
важна целостность, а не секретность. Целостность подтверждается сверкой одного
отпечатка голосом или лично; отпечаток покрывает все три ключа сразу, поэтому
читать вслух нужно одну строку, а не три ключа.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml


OPERATOR_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
SCHEMA_VERSION = 1


def fail(message: str) -> None:
    print(f"ошибка: {message}", file=sys.stderr)
    raise SystemExit(2)


def run(command: list[str], *, stdin: str | None = None) -> str:
    executable = shutil.which(command[0])
    if executable is None:
        fail(f"{command[0]} не найден в PATH")
    try:
        result = subprocess.run(
            [executable, *command[1:]],
            input=stdin,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        fail(f"не удалось выполнить {command[0]}: {exc}")
    if result.returncode != 0:
        diagnostic = result.stderr.strip().splitlines()
        fail(diagnostic[-1] if diagnostic else f"{command[0]} завершился с ошибкой")
    return result.stdout


def write_private(path: Path, content: str) -> None:
    """Пишет приватный материал с правами 0600, не давая перезаписать чужой.

    Открытие с O_EXCL — не осторожность, а условие: перезапись существующего
    ключа отняла бы доступ у уже включённой личности, и восстановить его было бы
    нечем.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        fail(
            f"{path} уже существует; перезапись отняла бы доступ у существующей "
            "личности. Удалите файл осознанно, если он больше не нужен"
        )
    except OSError as exc:
        fail(f"не удалось создать {path}: {exc}")
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)


def enrollment_fingerprint(request: dict[str, object]) -> str:
    """Один отпечаток на все три ключа, чтобы сверять вслух одну строку.

    Считается по канонической сериализации запроса, поэтому подмена любого из
    ключей меняет его целиком. Обе стороны считают его одинаково: выдающий
    оператор сверяет то, что видит у себя, с тем, что ему прочитали.
    """
    canonical = yaml.safe_dump(request, sort_keys=True, allow_unicode=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return " ".join(digest[index : index + 4] for index in range(0, 32, 4))


def generate(arguments: argparse.Namespace) -> None:
    operator = arguments.operator
    if not OPERATOR_RE.fullmatch(operator):
        fail("идентификатор оператора: строчные буквы, цифры и дефис, до 63 символов")

    home = Path(arguments.home).expanduser()
    age_identity = home / "sops" / "age-identity.txt"
    ssh_identity = home / "keys" / "operator-ssh"
    # Тот же путь, что уже ожидает Makefile в PLATFORM_WIREGUARD_PRIVATE_KEY.
    wireguard_identity = home / "keys" / "operator-wg.key"

    age_output = run(["age-keygen"])
    age_recipient = ""
    for line in age_output.splitlines():
        if line.startswith("# public key:"):
            age_recipient = line.split(":", 1)[1].strip()
    if not age_recipient:
        fail("age-keygen не сообщил публичный ключ")
    write_private(age_identity, age_output)

    # ssh-keygen не создаёт родительский каталог и не спрашивает — он просто
    # падает, поэтому каталог готовится здесь.
    ssh_identity.parent.mkdir(parents=True, exist_ok=True)
    if ssh_identity.exists():
        fail(
            f"{ssh_identity} уже существует; перезапись отняла бы доступ у "
            "существующей личности"
        )
    run(
        [
            "ssh-keygen",
            "-t", "ed25519",
            "-N", "",
            "-C", f"spiritvpn-operator-{operator}",
            "-f", str(ssh_identity),
        ]
    )
    # Не with_suffix: он заменил бы уже существующее расширение, а имя ключа
    # может его содержать. ssh-keygen всегда дописывает .pub к имени целиком.
    ssh_public = Path(f"{ssh_identity}.pub").read_text(encoding="utf-8").strip()
    os.chmod(ssh_identity, 0o600)

    wireguard_private = run(["wg", "genkey"]).strip()
    write_private(wireguard_identity, wireguard_private + "\n")
    wireguard_public = run(["wg", "pubkey"], stdin=wireguard_private + "\n").strip()
    try:
        if len(base64.b64decode(wireguard_public, validate=True)) != 32:
            raise ValueError
    except (ValueError, base64.binascii.Error):
        fail("wg pubkey вернул ключ неожиданной формы")

    request = {
        "schema_version": SCHEMA_VERSION,
        "operator": operator,
        "age_recipient": age_recipient,
        "ssh_public_key": ssh_public,
        "wireguard_public_key": wireguard_public,
    }
    fingerprint = enrollment_fingerprint(request)

    document = yaml.safe_dump(request, sort_keys=True, allow_unicode=True)
    destination = Path(arguments.output) if arguments.output else None
    if destination is None:
        sys.stdout.write(document)
    else:
        destination.write_text(document, encoding="utf-8")

    print(
        "\n".join(
            [
                "",
                "Личность создана. Приватные ключи остались только здесь:",
                f"  age        {age_identity}",
                f"  ssh        {ssh_identity}",
                f"  wireguard  {wireguard_identity}",
                "",
                "Запрос на включение содержит только публичные части и может быть",
                "передан любым каналом — важна целостность, а не секретность.",
                "",
                "Отпечаток включения — прочитайте его выдающему оператору голосом",
                "или лично. Он покрывает все три ключа сразу:",
                "",
                f"    {fingerprint}",
                "",
                "Без сверки отпечатка вне канала выдача доступа означает включение",
                "того, кто перехватил запрос.",
            ]
        ),
        file=sys.stderr,
    )


def verify(arguments: argparse.Namespace) -> None:
    path = Path(arguments.request)
    try:
        request = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        fail(f"{path}: {exc}")
    if not isinstance(request, dict):
        fail(f"{path}: запрос должен быть отображением")
    print(enrollment_fingerprint(request))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="создать личность и запрос на включение")
    create.add_argument("--operator", required=True, help="идентификатор оператора")
    create.add_argument("--output", help="файл запроса; по умолчанию stdout")
    create.add_argument(
        "--home",
        default="~/.config/spiritvpn",
        help="каталог приватного материала оператора",
    )
    create.set_defaults(handler=generate)

    show = commands.add_parser("fingerprint", help="посчитать отпечаток готового запроса")
    show.add_argument("--request", required=True)
    show.set_defaults(handler=verify)

    arguments = parser.parse_args()
    arguments.handler(arguments)


if __name__ == "__main__":
    main()
