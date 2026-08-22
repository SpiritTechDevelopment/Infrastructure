#!/usr/bin/env python3
"""Выдаёт и отзывает операторский доступ, правя только объявления в Git.

В Git попадают исключительно публичные части: SSH-ключ, публичный ключ
WireGuard и age-получатель. Приватный материал создаётся на машине владельца
скриптом operator-identity.py и сюда не передаётся никогда — поэтому отзыв
остаётся обычным отревьюенным изменением, а не ротацией всего, что успело
утечь в историю.

Скрипт ничего не применяет. Он оставляет диффф: контракт доступа к платформе
через обычный platform-deploy не проходит и применяется отдельной защищённой
операцией после ревью.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import ipaddress
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml


OPERATOR_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
AGE_RECIPIENT_RE = re.compile(r"^age1[0-9a-z]{58,}$")
SSH_KEY_RE = re.compile(r"^(ssh-ed25519|ecdsa-sha2-nistp256) [A-Za-z0-9+/=]+( .*)?$")
BUNDLE = Path("inventories/bootstrap/platform.sops.yml")
SOPS_CONFIG = Path(".sops.yaml")


def fail(message: str) -> None:
    print(f"ошибка: {message}", file=sys.stderr)
    raise SystemExit(2)


def run_sops(
    arguments: list[str], *, content: str | None = None, cwd: Path | None = None
) -> str:
    """Всегда выполняется из корня репозитория.

    `--filename-override` sops разрешает относительно текущего каталога, а не
    относительно --config. Без явного cwd вызов из любого другого каталога
    падал бы с «no matching creation rules found», и причина выглядела бы как
    поломка .sops.yaml, а не как рабочий каталог процесса.
    """
    try:
        result = subprocess.run(
            ["sops", *arguments],
            input=content,
            text=True,
            capture_output=True,
            check=False,
            cwd=str(cwd) if cwd else None,
        )
    except OSError as exc:
        fail(f"не удалось выполнить sops: {exc}")
    if result.returncode != 0:
        diagnostic = result.stderr.strip().splitlines()
        fail(diagnostic[-1] if diagnostic else "sops завершился с ошибкой")
    return result.stdout


def load_mapping(text: str, source: str) -> dict[str, Any]:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        fail(f"{source}: некорректный YAML: {exc}")
    if not isinstance(document, dict):
        fail(f"{source}: документ должен быть отображением")
    return document


def encrypted_files(root: Path) -> list[Path]:
    """Все файлы, подпадающие под creation_rules, кроме самого контракта.

    Перечисляются по правилам, а не списком: правило, о котором список не знает,
    и есть тот файл, который забудут перешифровать.
    """
    rules = creation_rules(root)
    candidates = sorted(
        {
            *(root / "desired").rglob("*.yml"),
            *(root / "desired").rglob("*.yaml"),
            *(root / "inventories").rglob("*.yml"),
            *(root / "inventories").rglob("*.yaml"),
        }
    )
    matched: list[Path] = []
    for path in candidates:
        relative = path.relative_to(root).as_posix()
        if any(pattern.search(relative) for pattern, _ in rules):
            matched.append(path)
    return matched


def creation_rules(root: Path) -> list[tuple[re.Pattern[str], list[str]]]:
    document = load_mapping(
        (root / SOPS_CONFIG).read_text(encoding="utf-8"), str(SOPS_CONFIG)
    )
    raw = document.get("creation_rules")
    if not isinstance(raw, list) or not raw:
        fail(f"{SOPS_CONFIG}: creation_rules должен быть непустым списком")
    rules: list[tuple[re.Pattern[str], list[str]]] = []
    for rule in raw:
        if not isinstance(rule, dict):
            fail(f"{SOPS_CONFIG}: правило должно быть отображением")
        recipients = [
            item.strip()
            for item in str(rule.get("age", "")).replace("\n", " ").split(",")
            if item.strip()
        ]
        rules.append((re.compile(str(rule.get("path_regex", ""))), recipients))
    return rules


def rewrite_sops_config(root: Path, *, add: str | None, remove: str | None) -> None:
    """Правит список получателей текстом, сохраняя комментарии файла.

    Перезапись через yaml.safe_dump стёрла бы объяснения, ради которых каждый
    получатель в этом файле и подписан, — а именно они не дают перепутать
    операторскую личность с личностью раннера.
    """
    path = root / SOPS_CONFIG
    lines = path.read_text(encoding="utf-8").splitlines()
    result: list[str] = []
    index = 0
    touched = 0
    while index < len(lines):
        line = lines[index]
        result.append(line)
        if not line.strip().startswith("age: >-"):
            index += 1
            continue

        # Блок свёрнутого скаляра собирается целиком и переписывается заново.
        # Построчная правка оставила бы висящую запятую при удалении последнего
        # получателя, а такой список sops прочитает как пустого получателя.
        index += 1
        recipients: list[str] = []
        indent = ""
        while index < len(lines) and lines[index].strip().startswith("age1"):
            raw = lines[index]
            indent = raw[: len(raw) - len(raw.lstrip())]
            recipients.append(raw.strip().rstrip(","))
            index += 1

        if remove:
            recipients = [item for item in recipients if item != remove]
        if add and add not in recipients:
            recipients.append(add)
        if not recipients:
            fail(f"{SOPS_CONFIG}: правило осталось бы без получателей")

        for position, recipient in enumerate(recipients):
            suffix = "," if position < len(recipients) - 1 else ""
            result.append(f"{indent}{recipient}{suffix}")
        touched += 1

    if touched == 0:
        fail(f"{SOPS_CONFIG}: не найдено ни одного блока получателей")
    path.write_text("\n".join(result) + "\n", encoding="utf-8")


ENVELOPE_KEYS = {"apiVersion", "kind", "inventory", "known_hosts", "vars"}


def read_bundle(root: Path) -> dict[str, Any]:
    """Возвращает конверт контракта как есть.

    Внутри конверта `inventory`, `known_hosts` и `vars` — зашифрованные
    **строки**, а не вложенные отображения: SOPS шифрует значения этих ключей
    целиком. Поэтому желаемое состояние разбирается вторым проходом, а обратно
    собирается сериализацией в строку. Обращение к `bundle["vars"]` как к
    словарю выглядит естественно и не работает.
    """
    target = root / BUNDLE
    if not target.is_file():
        fail(f"{BUNDLE}: контракт платформы не найден")
    envelope = load_mapping(run_sops(["--decrypt", str(target)], cwd=root), str(BUNDLE))
    if set(envelope) != ENVELOPE_KEYS:
        fail(f"{BUNDLE}: неожиданная структура конверта")
    if (
        envelope.get("apiVersion") != "spiritvpn.io/v1alpha1"
        or envelope.get("kind") != "PlatformBootstrap"
    ):
        fail(f"{BUNDLE}: неподдерживаемый тип документа")
    return envelope


def write_bundle(root: Path, document: dict[str, Any]) -> None:
    """Шифрует в памяти и пишет уже шифротекст.

    Расшифрованный контракт не появляется на диске ни на мгновение: в нём
    операторский ростер и границы доступа.
    """
    plaintext = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
    ciphertext = run_sops(
        [
            "--config", str(root / SOPS_CONFIG),
            "--filename-override", str(BUNDLE),
            "--encrypt",
            "--input-type", "yaml",
            "--output-type", "yaml",
            "/dev/stdin",
        ],
        content=plaintext,
        cwd=root,
    )
    (root / BUNDLE).write_text(ciphertext, encoding="utf-8")


def variables(bundle: dict[str, Any]) -> dict[str, Any]:
    raw = bundle.get("vars")
    if not isinstance(raw, str) or not raw.strip():
        fail(f"{BUNDLE}: раздел vars должен быть непустой строкой")
    values = load_mapping(raw, f"{BUNDLE}:vars")
    return values


def set_variables(bundle: dict[str, Any], values: dict[str, Any]) -> None:
    """Кладёт желаемое состояние обратно в конверт строкой.

    Порядок ключей сохраняется, поэтому диффф остаётся читаемым: перестановка
    полей превратила бы добавление одного оператора в переписывание файла.
    """
    bundle["vars"] = yaml.safe_dump(values, sort_keys=False, allow_unicode=True)


def validate_request(path: Path) -> dict[str, str]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        fail(f"{path}: {exc}")
    if not isinstance(document, dict):
        fail(f"{path}: запрос должен быть отображением")
    if document.get("schema_version") != 1:
        fail(f"{path}: поддерживается только schema_version: 1")
    request = {
        key: str(document.get(key, ""))
        for key in ("operator", "age_recipient", "ssh_public_key", "wireguard_public_key")
    }
    if not OPERATOR_RE.fullmatch(request["operator"]):
        fail("идентификатор оператора некорректен")
    if not AGE_RECIPIENT_RE.fullmatch(request["age_recipient"]):
        fail("age-получатель некорректен")
    if not SSH_KEY_RE.fullmatch(request["ssh_public_key"]):
        fail("SSH-ключ должен быть ssh-ed25519 или ecdsa-sha2-nistp256")
    try:
        if len(base64.b64decode(request["wireguard_public_key"], validate=True)) != 32:
            raise ValueError
    except (ValueError, binascii.Error):
        fail("публичный ключ WireGuard должен быть 32 байтами в base64")
    return request


def confirm(request: dict[str, str], address: str, assume_verified: bool) -> None:
    fingerprint_input = {
        "schema_version": 1,
        "operator": request["operator"],
        "age_recipient": request["age_recipient"],
        "ssh_public_key": request["ssh_public_key"],
        "wireguard_public_key": request["wireguard_public_key"],
    }
    import hashlib

    canonical = yaml.safe_dump(fingerprint_input, sort_keys=True, allow_unicode=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    fingerprint = " ".join(digest[index : index + 4] for index in range(0, 32, 4))

    print(f"оператор            {request['operator']}", file=sys.stderr)
    print(f"адрес в оверлее     {address}", file=sys.stderr)
    print(f"отпечаток включения {fingerprint}", file=sys.stderr)
    print("", file=sys.stderr)
    if assume_verified:
        print("отпечаток объявлен сверенным флагом; сверка вне канала пропущена", file=sys.stderr)
        return
    print(
        "Сверьте отпечаток с владельцем ключей голосом или лично. Без этого\n"
        "выдача включает того, кто перехватил запрос, а не того, кого вы ждёте.",
        file=sys.stderr,
    )
    answer = input("отпечаток сверён вне этого канала? введите yes: ").strip()
    if answer != "yes":
        fail("выдача отменена: отпечаток не подтверждён")


def grant(root: Path, arguments: argparse.Namespace) -> None:
    request = validate_request(Path(arguments.request))
    bundle = read_bundle(root)
    values = variables(bundle)

    operators = values.get("platform_operator_ssh_public_keys")
    peers = values.get("platform_wireguard_operator_peers")
    if not isinstance(operators, list) or not isinstance(peers, list):
        fail(f"{BUNDLE}: операторский ростер имеет неожиданную форму")

    if any(peer.get("id") == request["operator"] for peer in peers):
        fail(f"оператор {request['operator']} уже включён")
    if request["ssh_public_key"] in operators:
        fail("такой SSH-ключ уже присутствует в ростере")
    if any(peer.get("public_key") == request["wireguard_public_key"] for peer in peers):
        fail("такой публичный ключ WireGuard уже присутствует в ростере")

    networks = [
        ipaddress.ip_interface(value)
        for value in values.get("platform_wireguard_hub_addresses", {}).values()
    ]
    try:
        address = ipaddress.ip_address(arguments.address)
    except ValueError:
        fail("адрес в оверлее некорректен")
    if not any(address in network.network for network in networks):
        fail("адрес вне управляющих сетей контракта")
    if any(address == network.ip for network in networks):
        fail("адрес занят самим хабом")
    taken = {
        ipaddress.ip_network(cidr).network_address
        for peer in peers
        for cidr in peer.get("allowed_ips", [])
    }
    if address in taken:
        fail("адрес уже выдан другому оператору")

    confirm(request, str(address), arguments.assume_verified)

    operators.append(request["ssh_public_key"])
    peers.append(
        {
            "id": request["operator"],
            "public_key": request["wireguard_public_key"],
            "allowed_ips": [f"{address}/32"],
        }
    )

    # Порядок обязателен: получатель добавляется в .sops.yaml до перешифровки,
    # иначе контракт был бы записан без него и новый оператор не открыл бы даже
    # тот файл, который его включает.
    set_variables(bundle, values)
    rewrite_sops_config(root, add=request["age_recipient"], remove=None)
    write_bundle(root, bundle)
    update_keys(root)
    report(root, request["operator"], str(address), granted=True)


def revoke(root: Path, arguments: argparse.Namespace) -> None:
    operator = arguments.operator
    if not OPERATOR_RE.fullmatch(operator):
        fail("идентификатор оператора некорректен")
    bundle = read_bundle(root)
    values = variables(bundle)
    operators = values.get("platform_operator_ssh_public_keys")
    peers = values.get("platform_wireguard_operator_peers")
    if not isinstance(operators, list) or not isinstance(peers, list):
        fail(f"{BUNDLE}: операторский ростер имеет неожиданную форму")

    peer = next((item for item in peers if item.get("id") == operator), None)
    if peer is None:
        fail(f"оператор {operator} в ростере не найден")
    if len(peers) == 1:
        fail("нельзя отозвать последнего оператора: доступ к платформе был бы утрачен")

    comment = f"spiritvpn-operator-{operator}"
    remaining = [key for key in operators if not key.strip().endswith(comment)]
    if len(remaining) == len(operators):
        fail(
            f"SSH-ключ оператора {operator} не опознан по комментарию {comment!r}; "
            "удалите его из ростера вручную и повторите"
        )
    values["platform_operator_ssh_public_keys"] = remaining
    values["platform_wireguard_operator_peers"] = [
        item for item in peers if item.get("id") != operator
    ]

    recipient = arguments.age_recipient
    if recipient and not AGE_RECIPIENT_RE.fullmatch(recipient):
        fail("age-получатель некорректен")

    # Порядок обратен выдаче и так же обязателен: получатель убирается из
    # .sops.yaml до записи контракта. Иначе контракт с уже вычищенным ростером
    # на короткое время лежал бы на диске зашифрованным ещё и на отзываемого, и
    # прерывание процесса здесь оставило бы отзыв незавершённым и незаметным.
    set_variables(bundle, values)
    if recipient:
        rewrite_sops_config(root, add=None, remove=recipient)
    write_bundle(root, bundle)
    if recipient:
        update_keys(root)
    report(root, operator, None, granted=False, recipient_removed=bool(recipient))


def hosts(root: Path, arguments: argparse.Namespace) -> None:
    """Печатает строки для /etc/hosts оператора.

    Имена берутся под `.internal`, а не под выдуманным TLD: `.internal`
    зарезервирован ICANN под частное использование и никогда не будет
    делегирован, тогда как произвольный TLD однажды может стать настоящим — и
    тогда запрос, ушедший мимо туннеля, ответит кто-то чужой. Тот же суффикс
    уже стоит в SAN сертификата Vault, поэтому имя работает без перевыпуска.

    Служба DNS на хабе ради двух имён не нужна: это ещё один компонент с
    digest'ом и ещё одна поверхность на машине, которая держит Vault.
    """
    bundle = read_bundle(root)
    values = variables(bundle)
    addresses = values.get("platform_wireguard_hub_addresses")
    if not isinstance(addresses, dict) or not addresses:
        fail(f"{BUNDLE}: в контракте нет адресов хаба")
    environment = arguments.environment
    if environment not in addresses:
        fail(f"среда {environment} в контракте не объявлена")
    address = str(addresses[environment]).split("/")[0]

    names = [
        str(values.get("platform_vault_tls_server_name") or "vault.management.internal"),
        "grafana.management.internal",
    ]
    print(f"# spiritvpn: управляющий оверлей, среда {environment}")
    print(f"{address}\t{' '.join(names)}")
    print(
        "\n".join(
            [
                "",
                "Вставьте строку выше в /etc/hosts. Имена резолвятся только при",
                "поднятом туннеле — адрес принадлежит управляющему оверлею.",
                "",
                "  Vault UI    https://%s:%s" % (names[0], values.get("platform_vault_api_port", 8200)),
                "  Grafana     http://grafana.management.internal:3000",
                "",
                "Vault отдаёт TLS, подписанный локальным CA управляющего хоста;",
                "его сертификат нужно один раз добавить в доверенные. Grafana",
                "работает по HTTP внутри туннеля: WireGuard шифрует участок",
                "оператор—хаб, и другие peer'ы этот трафик не видят.",
            ]
        ),
        file=sys.stderr,
    )


def update_keys(root: Path) -> None:
    for path in encrypted_files(root):
        run_sops(["updatekeys", "--yes", str(path)], cwd=root)


def report(
    root: Path,
    operator: str,
    address: str | None,
    *,
    granted: bool,
    recipient_removed: bool = True,
) -> None:
    action = "выдан" if granted else "отозван"
    lines = [
        "",
        f"Доступ {action} в объявлениях. Ничего не применено — это диффф на ревью.",
        "",
        "Что изменилось:",
        f"  {SOPS_CONFIG}",
        f"  {BUNDLE}",
        "  перешифрованы все файлы, подпадающие под creation_rules",
        "",
    ]
    if granted:
        lines += [
            f"Что получил {operator}:",
            "  расшифровку желаемого состояния и контракта платформы;",
            f"  SSH на управляющий хост и peer в оверлее с адресом {address}.",
            "",
            "Чего он НЕ получил и что выдаётся отдельно:",
            "  политику Vault — она в этом контракте не описана вовсе.",
            "",
        ]
    elif not recipient_removed:
        lines += [
            "ВНИМАНИЕ: age-получатель не назван и остался в .sops.yaml.",
            "Отозванный оператор продолжает расшифровывать всё. Повторите с",
            "--age-recipient, иначе отзыв неполон.",
            "",
        ]
    lines += [
        "Дальше:",
        "  make check                     — проверить конверты и набор получателей",
        "  git diff                       — прочитать изменение",
        "  коммит, pull request, ревью по CODEOWNERS",
        "",
        "После слияния в main контракт применяет обычная выкатка платформы:",
        "для develop она запускается сама, для prod — запуском platform-deploy",
        "с точным SHA. Пока прогон не прошёл, изменение не состоялось.",
        "",
        "Ревью здесь — единственный гейт. Слитый коммит меняет и ростер SSH,",
        "то есть тех, у кого root на управляющем сервере.",
    ]
    print("\n".join(lines), file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)

    grant_command = commands.add_parser("grant", help="включить оператора")
    grant_command.add_argument("--request", required=True, help="файл запроса на включение")
    grant_command.add_argument(
        "--address",
        required=True,
        help=(
            "адрес оператора в управляющем оверлее. Обязателен намеренно: адреса "
            "нод флота живут вне этого контракта, поэтому автоматический выбор мог "
            "бы молча столкнуться с адресом ноды"
        ),
    )
    grant_command.add_argument(
        "--assume-verified",
        action="store_true",
        help="пропустить сверку отпечатка; только для тестов",
    )
    grant_command.set_defaults(handler=grant)

    revoke_command = commands.add_parser("revoke", help="отозвать оператора")
    revoke_command.add_argument("--operator", required=True)
    revoke_command.add_argument(
        "--age-recipient",
        help="age-получатель отзываемого оператора; без него отзыв неполон",
    )
    revoke_command.set_defaults(handler=revoke)

    hosts_command = commands.add_parser(
        "hosts", help="строки /etc/hosts для внутренних имён"
    )
    hosts_command.add_argument("--environment", default="develop")
    hosts_command.set_defaults(handler=hosts)

    arguments = parser.parse_args()
    arguments.handler(arguments.root.resolve(), arguments)


if __name__ == "__main__":
    main()
