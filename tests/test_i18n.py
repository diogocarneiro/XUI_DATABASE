# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Translations: every message translated, placeholders intact, switching works."""

from __future__ import annotations

import pytest

from xdbtool import i18n
from xdbtool.i18n import LANGUAGES, SOURCE_LANGUAGE, _, set_language
from xdbtool.i18n.extract import fields, load, messages

TRANSLATED = [c for c in LANGUAGES if c != SOURCE_LANGUAGE]
MESSAGES = messages()


@pytest.fixture(autouse=True)
def _english_after():
    yield
    set_language(SOURCE_LANGUAGE)


def test_portuguese_is_the_default_and_all_five_languages_exist():
    assert i18n.DEFAULT_LANGUAGE == "pt"
    assert set(LANGUAGES) == {"pt", "en", "fr", "es", "it"}


@pytest.mark.parametrize("code", TRANSLATED)
def test_catalog_covers_every_message(code):
    catalog = load(code)
    assert MESSAGES - catalog.keys() == set(), f"{code}: untranslated messages"
    assert catalog.keys() - MESSAGES == set(), f"{code}: stale entries (run extract --update)"
    assert all(v.strip() for v in catalog.values()), f"{code}: empty translation"


@pytest.mark.parametrize("code", TRANSLATED)
def test_placeholders_match_english(code):
    bad = {k: v for k, v in load(code).items() if fields(k) != fields(v)}
    assert not bad, f"{code}: placeholders differ: {bad}"


@pytest.mark.parametrize("code", TRANSLATED)
def test_every_translation_formats(code):
    set_language(code)
    for key in MESSAGES:
        sample = {name: 0.5 if name == "confidence" else "x" for name in fields(key)}
        _(key, **sample)  # raises on a broken placeholder or format spec


def test_switching_language():
    set_language("pt")
    assert _("Connect / Load") == "Ligar / Carregar"
    assert _("{table}: {count} rows", table="lines", count=485) == "lines: 485 linhas"
    set_language("fr")
    assert _("Maintenance") == "Maintenance"
    assert _("Movies") == "Films"
    set_language("xx")  # unknown → English
    assert i18n.get_language() == "en"
    assert _("Movies") == "Movies"


def test_untranslated_text_falls_back_to_english():
    set_language("es")
    assert _("not a catalogued message {n}", n=3) == "not a catalogued message 3"


def test_core_reports_follow_the_language():
    from xdbtool.core.xui_migrate import prepare_dump

    dump = "CREATE TABLE `users` (`id` int);\nCREATE TABLE `streams` (`id` int);\n"
    set_language("it")
    assert prepare_dump(dump).summary_lines()[0] == "Tabelle nel dump: 2"
    set_language("en")
    assert prepare_dump(dump).summary_lines()[0] == "Tables in dump: 2"


def test_cli_lang_option(capsys, tmp_path):
    from xdbtool.__main__ import main

    dump = tmp_path / "x.sql"
    dump.write_text("CREATE TABLE `users` (`id` int);\nCREATE TABLE `streams` (`id` int);\n",
                    encoding="utf-8")
    assert main(["--lang", "es", "xui-migrate", str(dump), "--check-only"]) == 0
    assert "Tablas en el dump: 2" in capsys.readouterr().out
    assert main(["xui-migrate", str(dump), "--check-only"]) == 0   # CLI default: English
    assert "Tables in dump: 2" in capsys.readouterr().out


def test_settings_remember_language():
    from xdbtool.core.config import load_settings, save_settings

    save_settings(language="fr")
    assert load_settings()["language"] == "fr"
