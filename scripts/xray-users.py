#!/usr/bin/env python3
"""Extract exact Xray inbound user e-mail/accounting IDs from API output.

`xray api inbounduser` output has changed formatting between releases. This parser
accepts JSON/proto-JSON recursively and has a conservative text fallback. It never
uses substring matching, so `user-1` cannot accidentally match `user-10`.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any, Iterable

SAFE_ID = re.compile(r"^[A-Za-z0-9._@:+-]{1,128}$")
TEXT_EMAIL = re.compile(
    r"(?im)(?:^|[,{\s])(?:email|Email)\s*[:=]\s*[\"']?([A-Za-z0-9._@:+-]{1,128})"
)


def walk(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() == "email" and isinstance(item, str):
                yield item
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def extract(text: str) -> list[str]:
    found: set[str] = set()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if parsed is not None:
        for candidate in walk(parsed):
            candidate = candidate.strip()
            if SAFE_ID.fullmatch(candidate):
                found.add(candidate)
    for match in TEXT_EMAIL.finditer(text):
        candidate = match.group(1)
        if SAFE_ID.fullmatch(candidate):
            found.add(candidate)
    return sorted(found)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("list", "has"))
    parser.add_argument("email", nargs="?")
    args = parser.parse_args()
    text = sys.stdin.read()
    users = extract(text)
    if args.action == "list":
        for user in users:
            print(user)
        return 0
    if not args.email or not SAFE_ID.fullmatch(args.email):
        parser.error("has requires one valid exact email/accounting identifier")
    return 0 if args.email in users else 1


if __name__ == "__main__":
    raise SystemExit(main())
