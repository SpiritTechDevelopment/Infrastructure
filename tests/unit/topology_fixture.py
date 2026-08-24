"""Доступ к объявлениям фикстуры, которые теперь лежат одним `topology.yml`.

Раскладка по объектам (`environment.yml`, `nodes/`, `fleets/`, `instances/`)
снята: и боевой desired state, и фикстура держат окружение одним бандлом.
Тесты правили те файлы напрямую, и без этого модуля каждый из них превратился
бы в возню с индексами внутри `spec.objects`.

Обращение идёт по идентификатору объекта, потому что имя файла им и было:
`nodes/develop-exit-de.yml` — это объект `develop-exit-de`. Отображение
однозначно, так что тесты читаются так же, как читались.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import yaml


def path(desired_root: Path, environment: str = "develop") -> Path:
    return desired_root / "environments" / environment / "topology.yml"


def load(desired_root: Path, environment: str = "develop") -> dict[str, Any]:
    return yaml.safe_load(path(desired_root, environment).read_text(encoding="utf-8"))


class _IndentingDumper(yaml.SafeDumper):
    """Отбивает элементы списка от ключа, как того требует yamllint.

    Тесты пишут во временные каталоги, которые линтер не видит, но записанный
    бандл иногда читают глазами при разборе падения. Расхождение формы с
    фикстурой в репозитории на ровном месте сбивает.
    """

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> Any:
        return super().increase_indent(flow, False)


def save(desired_root: Path, bundle: dict[str, Any], environment: str = "develop") -> None:
    path(desired_root, environment).write_text(
        yaml.dump(
            bundle,
            Dumper=_IndentingDumper,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        ),
        encoding="utf-8",
    )


def get(desired_root: Path, object_id: str, environment: str = "develop") -> dict[str, Any]:
    for document in load(desired_root, environment)["spec"]["objects"]:
        if document["metadata"]["id"] == object_id:
            return document
    raise AssertionError(f"в бандле {environment} нет объекта {object_id!r}")


def put(desired_root: Path, document: dict[str, Any], environment: str = "develop") -> None:
    """Кладёт объект: заменяет одноимённый или добавляет новый."""
    bundle = load(desired_root, environment)
    objects = bundle["spec"]["objects"]
    object_id = document["metadata"]["id"]
    for index, existing in enumerate(objects):
        if existing["metadata"]["id"] == object_id:
            objects[index] = document
            break
    else:
        objects.append(document)
    save(desired_root, bundle, environment)


def drop(desired_root: Path, object_id: str, environment: str = "develop") -> None:
    bundle = load(desired_root, environment)
    objects = bundle["spec"]["objects"]
    remaining = [item for item in objects if item["metadata"]["id"] != object_id]
    if len(remaining) == len(objects):
        raise AssertionError(f"в бандле {environment} нет объекта {object_id!r}")
    bundle["spec"]["objects"] = remaining
    save(desired_root, bundle, environment)


def edit(
    desired_root: Path,
    object_id: str,
    mutate: Callable[[dict[str, Any]], None],
    environment: str = "develop",
) -> None:
    document = get(desired_root, object_id, environment)
    mutate(document)
    put(desired_root, document, environment)
