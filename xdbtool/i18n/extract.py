# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Find every translatable message in the package and check the catalogs.

    python -m xdbtool.i18n.extract            # report missing / unused per language
    python -m xdbtool.i18n.extract --update   # add missing keys (value = English) and
                                              # drop unused ones, keeping translations

A message is the literal first argument of ``_()`` or ``N_()``.
"""

from __future__ import annotations

import argparse
import ast
import json
import string
from pathlib import Path

from . import LANGUAGES, LOCALES_DIR, SOURCE_LANGUAGE

PACKAGE_DIR = Path(__file__).resolve().parent.parent


def messages(package_dir: Path = PACKAGE_DIR) -> set[str]:
    found: set[str] = set()
    for path in package_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in ("_", "N_") and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                found.add(node.args[0].value)
    return found


def fields(text: str) -> set[str]:
    """Named placeholders in a message (``{rows}`` / ``{pct:.0%}`` → ``rows`` / ``pct``)."""
    return {name for _lit, name, _spec, _conv in string.Formatter().parse(text) if name}


def load(code: str) -> dict[str, str]:
    path = LOCALES_DIR / f"{code}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def save(code: str, data: dict[str, str]) -> None:
    text = json.dumps(dict(sorted(data.items())), ensure_ascii=False, indent=2)
    (LOCALES_DIR / f"{code}.json").write_text(text + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--update", action="store_true")
    args = ap.parse_args(argv)
    keys = messages()
    print(f"{len(keys)} messages in the code")
    problems = 0
    for code in LANGUAGES:
        if code == SOURCE_LANGUAGE:
            continue
        cat = load(code)
        missing = sorted(keys - cat.keys())
        unused = sorted(cat.keys() - keys)
        print(f"  {code}: {len(cat)} entries, {len(missing)} missing, {len(unused)} unused")
        problems += len(missing)
        if args.update:
            save(code, {**{k: cat.get(k, k) for k in keys}})
    return 1 if problems and not args.update else 0


if __name__ == "__main__":
    raise SystemExit(main())
