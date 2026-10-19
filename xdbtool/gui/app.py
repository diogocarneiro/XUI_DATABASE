# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""QApplication bootstrap."""

from __future__ import annotations

import sys

from ..core.config import load_settings
from ..core.logger import setup_logging
from ..i18n import DEFAULT_LANGUAGE, set_language


def run(language: str | None = None) -> int:
    setup_logging()
    # Explicit choice (CLI --lang) > saved preference > Portuguese.
    set_language(language or load_settings().get("language") or DEFAULT_LANGUAGE)
    from PySide6.QtWidgets import QApplication  # imported lazily so CLI works headless

    from .main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("XUI DB Tool")
    window = MainWindow()
    app._xdb_window = window  # replaced when the language is switched
    window.show()
    return app.exec()
