# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Theme tokens and stylesheet (pure Python — no display or PySide6 needed)."""

from __future__ import annotations

import re

import pytest

from xdbtool.gui import theme


def _luminance(hex_colour: str) -> float:
    rgb = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_modes_and_resolution():
    assert theme.MODES == ("auto", "light", "dark") and theme.DEFAULT_MODE == "auto"
    assert theme.resolve("light", system_dark=True) == "light"
    assert theme.resolve("dark", system_dark=False) == "dark"
    assert theme.resolve("auto", system_dark=True) == "dark"
    assert theme.resolve("auto", system_dark=False) == "light"


def test_both_schemes_define_the_same_tokens():
    assert theme.TOKENS["light"].keys() == theme.TOKENS["dark"].keys()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_text_is_readable(scheme):
    t = theme.TOKENS[scheme]
    # WCAG AA: 4.5:1 for body text, 3:1 for secondary text and UI accents.
    for bg in ("bg", "surface", "input", "log_bg"):
        assert contrast(t["text"], t[bg]) >= 4.5, (scheme, bg)
    for bg in ("bg", "surface"):
        assert contrast(t["muted"], t[bg]) >= 4.5, (scheme, "muted", bg)
        assert contrast(t["danger"], t[bg]) >= 3, (scheme, "danger", bg)
    assert contrast(t["accent_text"], t["accent"]) >= 3, scheme


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_stylesheet_is_fully_substituted(scheme, tmp_path):
    icons = theme.write_icons(scheme, tmp_path)
    qss = theme.stylesheet(scheme, icons)
    assert theme.TOKENS[scheme]["accent"] in qss
    assert not re.search(r"\{[a-z_]+\}", qss), "unsubstituted token"
    assert qss.count("{") == qss.count("}")
    for path in icons.values():
        assert path in qss and "\\" not in path
    assert 'QPushButton[role="primary"]' in qss and 'QPushButton[role="danger"]' in qss


def test_icons_are_written_once_and_coloured_per_scheme(tmp_path):
    light = theme.write_icons("light", tmp_path)
    dark = theme.write_icons("dark", tmp_path)
    assert light["check"] != dark["check"]
    svg = (tmp_path / "chevron-down-dark.svg").read_text(encoding="utf-8")
    assert theme.TOKENS["dark"]["muted"] in svg and svg.startswith("<svg")
    mtime = (tmp_path / "check-light.svg").stat().st_mtime_ns
    theme.write_icons("light", tmp_path)                   # unchanged → not rewritten
    assert (tmp_path / "check-light.svg").stat().st_mtime_ns == mtime
