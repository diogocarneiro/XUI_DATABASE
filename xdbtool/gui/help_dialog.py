# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""User guide and About dialogs."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QTextBrowser,
    QVBoxLayout,
)

from .. import about
from ..i18n import _, get_language


class HelpDialog(QDialog):
    """The user guide in the interface language; ``section`` scrolls to an anchor."""

    def __init__(self, parent=None, section: str = ""):
        super().__init__(parent)
        self.setWindowTitle(_("User guide"))
        self.resize(860, 680)
        layout = QVBoxLayout(self)
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)   # Telegram, GitHub… open in the browser
        self.browser.setHtml(about.help_html(get_language()))
        layout.addWidget(self.browser)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText(_("Close"))
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if section:
            self.browser.scrollToAnchor(section)


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("About XUI DB Tool"))
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        title = QLabel(f"{about.APP_NAME}  <span style='font-weight:400'>v{about.VERSION}</span>")
        title.setObjectName("appTitle")
        layout.addWidget(title)
        text = QLabel(
            "<p>" + _("Inspect, back up, repair and migrate XUI / Xtream-UI / StreamCreed panel "
                      "databases you own or are authorised to manage.") + "</p>"
            "<p>" + _("Credentials are migrated verbatim; hashes are never decrypted.") + "</p>"
            f"<p><b>{_('Developer')}:</b> {about.DEVELOPER}<br>"
            f"Telegram: <a href='{about.TELEGRAM}'>{about.TELEGRAM}</a><br>"
            f"Email: <a href='mailto:{about.EMAIL}'>{about.EMAIL}</a><br>"
            f"GitHub: <a href='{about.REPOSITORY}'>{about.REPOSITORY}</a></p>"
            f"<p>Copyright © 2026 {about.DEVELOPER} · {_('MIT License.')}</p>"
        )
        text.setWordWrap(True)
        text.setTextFormat(Qt.RichText)
        text.setOpenExternalLinks(True)
        text.setTextInteractionFlags(Qt.TextBrowserInteraction)
        text.setMinimumWidth(460)
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText(_("Close"))
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
