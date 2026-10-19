# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Project and developer details shown in the About dialog, the guide and the CLI."""

from __future__ import annotations

from pathlib import Path

from . import __version__

APP_NAME = "XUI DB Tool"
VERSION = __version__
DEVELOPER = "Diogo Carneiro"
EMAIL = "diogoncarneiro@outlook.com"
TELEGRAM = "https://t.me/diogoncarneiro"
REPOSITORY = "https://github.com/diogocarneiro/XUI_DATABASE"
ISSUES = f"{REPOSITORY}/issues"
RELEASES = f"{REPOSITORY}/releases"
LICENSE = "MIT"

HELP_DIR = Path(__file__).with_name("help")
# Sections every guide must have, in this order (anchors in help/<lang>.html).
HELP_SECTIONS = (
    "start", "source", "ssh", "export", "maintenance", "templates", "migrate",
    "xui-migrate", "xui-import", "safety", "preferences", "updates", "problems",
    "developer",
)


def help_html(language: str) -> str:
    """The user guide in ``language`` (English when it has no guide)."""
    path = HELP_DIR / f"{language}.html"
    if not path.is_file():
        path = HELP_DIR / "en.html"
    return path.read_text(encoding="utf-8").format(
        version=VERSION, telegram=TELEGRAM, email=EMAIL, repository=REPOSITORY,
        issues=ISSUES, releases=RELEASES, developer=DEVELOPER)
