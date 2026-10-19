# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""The user guide exists in every language, with the same sections and working links."""

from __future__ import annotations

import re

import pytest

from xdbtool import about
from xdbtool.i18n import LANGUAGES


@pytest.mark.parametrize("code", list(LANGUAGES))
def test_guide_has_every_section_in_order(code):
    assert (about.HELP_DIR / f"{code}.html").is_file(), f"no guide for {code}"
    html = about.help_html(code)
    anchors = re.findall(r'<a name="([\w-]+)"></a>', html)
    assert tuple(anchors) == about.HELP_SECTIONS


@pytest.mark.parametrize("code", list(LANGUAGES))
def test_guide_links_and_developer_contacts(code):
    html = about.help_html(code)
    assert "{" not in html and "}" not in html          # every placeholder was filled
    internal = set(re.findall(r'href="#([\w-]+)"', html))
    assert internal <= set(about.HELP_SECTIONS), internal - set(about.HELP_SECTIONS)
    assert f'href="{about.TELEGRAM}"' in html
    assert about.TELEGRAM == "https://t.me/diogoncarneiro"
    assert about.ISSUES in html and about.VERSION in html


def test_unknown_language_falls_back_to_english():
    assert about.help_html("xx") == about.help_html("en")
