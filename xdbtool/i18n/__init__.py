# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Translations for user-facing text (GUI, reports, error hints).

The English text is the message key (gettext style): ``_("Connect / Load")``.
Catalogs live in ``locales/<code>.json`` as ``{english: translation}``; a missing
entry falls back to English. Placeholders are always *named* (``{rows}``) so a
translation can reorder them, and are filled by keyword: ``_("{n} rows", n=5)``.

Portuguese is the default for the GUI. The CLI stays in English unless ``--lang``
is given, so scripts and logs remain stable. This module has no Qt dependency.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

LANGUAGES: dict[str, str] = {
    "pt": "Português",
    "en": "English",
    "fr": "Français",
    "es": "Español",
    "it": "Italiano",
}
DEFAULT_LANGUAGE = "pt"
SOURCE_LANGUAGE = "en"
LOCALES_DIR = Path(__file__).with_name("locales")

_current = SOURCE_LANGUAGE


@cache
def catalog(code: str) -> dict[str, str]:
    """The ``{english: translation}`` table for ``code`` ({} for English/unknown)."""
    path = LOCALES_DIR / f"{code}.json"
    if code == SOURCE_LANGUAGE or not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def set_language(code: str | None) -> str:
    """Switch the active language; unknown codes fall back to English."""
    global _current
    _current = code if code in LANGUAGES else SOURCE_LANGUAGE
    return _current


def get_language() -> str:
    return _current


def _(text: str, /, **fields) -> str:
    """Translate ``text`` into the active language, then fill ``{named}`` fields."""
    out = catalog(_current).get(text) or text
    return out.format(**fields) if fields else out


def N_(text: str) -> str:  # noqa: N802 - gettext convention
    """Mark ``text`` for translation without translating it yet (module constants)."""
    return text
