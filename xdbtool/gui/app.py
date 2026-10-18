"""QApplication bootstrap."""

from __future__ import annotations

import sys

from ..core.logger import setup_logging


def run() -> int:
    setup_logging()
    from PySide6.QtWidgets import QApplication  # imported lazily so CLI works headless

    from .main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("XUI DB Tool")
    window = MainWindow()
    window.show()
    return app.exec()
